"""Releases: promote verified integration-branch heads onto the release branch, on a schedule.

The factory merges into a repo's integration branch (`repos.base_branch`, `develop` on a repo
that has one) as fast as reviews pass. Nobody can look at a target that moves that often, so
at a few fixed moments of the day this module takes the integration head, waits for CI to be
green on that exact commit, and fast-forwards the release branch (`test`) onto it. That
commit is the release; the `release_items` rows say which factory-built issues it carried.

Then the repo is **frozen**: no further release is assembled until a human has marked this one
`reviewed`, or it has stood unread for `FACTORY_RELEASE_REVIEW_TIMEOUT`. The freeze is what
makes the release reviewable — the test server holds still while somebody looks at it. It
does not stop merges into the integration branch; the release is pinned by sha, and the
next one picks up everything that landed meanwhile.

Everything is derived from the `releases` table, so a restart neither repeats a moment nor
loses one: the schedule asks whether anything happened to the latest release since the most
recent moment (`due_moment`), and the freeze is a property of that same row (`frozen`).

Contains no model call, like the rest of `control/`. Promotion is two GitHub API calls.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Iterable
from zoneinfo import ZoneInfo

import httpx

from . import db, github, repos
from .config import settings

log = logging.getLogger("factory.release")

# States in which the control plane is doing something to the release, as opposed to the
# release standing still and waiting. A control plane that restarts mid-way leaves a row in
# one of these with nothing behind it; `recover` fails them, or the freeze would hold forever.
IN_FLIGHT = ("assembling", "deploying", "accepting")

# Standing states the clock may expire. `rejected` is not one: a browser said the version is
# broken, and that waits for a person.
EXPIRABLE = ("promoted", "deployed", "accepted")

# How much of each pull request's body the packet quotes.
SUMMARY_MAX = 400

_task: asyncio.Task | None = None
# Release id -> the task finishing it (the CI wait and the promotion).
_inflight: dict[int, asyncio.Task] = {}
# Repo -> the last moment the schedule evaluated for it and found nothing to release. Memory,
# not the table, because "nothing to do" writes no row; a restart re-asks once, harmlessly.
_checked: dict[str, dt.datetime] = {}


# --------------------------------------------------------------------------- pure decisions


def parse_times(raw: str) -> tuple[dt.time, ...]:
    """`"09:00,11:30"` as sorted times. Entries that are not `HH:MM` are dropped, not fatal."""
    out = set()
    for entry in (raw or "").split(","):
        entry = entry.strip()
        try:
            hour, minute = entry.split(":")
            out.add(dt.time(int(hour), int(minute)))
        except ValueError:
            if entry:
                log.warning("FACTORY_RELEASE_TIMES entry %r is not HH:MM; ignored", entry)
    return tuple(sorted(out))


def parse_ts(value: str) -> dt.datetime:
    """A stored timestamp as an aware UTC datetime. Naive values are taken to be UTC."""
    parsed = dt.datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def due_moment(
    now: dt.datetime,
    times: Iterable[dt.time],
    tz: str | dt.tzinfo,
    last_activity: str | None,
) -> dt.datetime | None:
    """The scheduled moment a release is owed for, or None.

    The most recent moment at or before `now` — wall-clock times in `tz`, so 09:00 stays 09:00
    across a daylight-saving change — provided nothing has happened to the latest release since
    it. `last_activity` is that release's last update: created, promoted, failed, reviewed or
    expired after the moment means the moment has been spent, either on a release or on a
    freeze that was still holding when it came round. That rule is what makes a restart safe
    (nothing is repeated) and what makes the moments mean something (a release reviewed at
    10:00 waits for 11:30, rather than firing the 09:00 one late).

    Looks back over today and yesterday only: past midnight, the moment owed is yesterday's
    last one, and nothing older is ever owed twice.
    """
    zone = ZoneInfo(tz) if isinstance(tz, str) else tz
    times = tuple(times)
    if not times:
        return None
    local_day = now.astimezone(zone).date()
    latest: dt.datetime | None = None
    for back in (0, 1):
        day = local_day - dt.timedelta(days=back)
        for t in times:
            # Through UTC, so a wall time that does not exist on a spring-forward day still
            # lands on a real instant rather than comparing as something it is not.
            moment = dt.datetime.combine(day, t, tzinfo=zone).astimezone(dt.timezone.utc)
            if moment <= now and (latest is None or moment > latest):
                latest = moment
        if latest is not None:
            break
    if latest is None:
        return None
    if last_activity and parse_ts(last_activity) >= latest:
        return None
    return latest


def _standing_since(release: dict) -> dt.datetime:
    return parse_ts(release.get("promoted_at") or release["created_at"])


def stale(release: dict, now: dt.datetime, timeout: int) -> bool:
    """Whether a standing release has waited for a human longer than `timeout` seconds."""
    return (
        release.get("status") in EXPIRABLE
        and timeout > 0
        and (now - _standing_since(release)).total_seconds() > timeout
    )


def frozen(release: dict | None, now: dt.datetime, timeout: int) -> bool:
    """Whether `release` — a repo's latest — holds that repo's next release back.

    Every non-terminal state freezes: the control plane is still working on it, or it is on
    the test server waiting to be read. A standing release past its timeout does not, because
    the next `expire_stale` turns it terminal and the repo should not wait a tick for that.
    """
    if release is None or release.get("status") in db.RELEASE_TERMINAL:
        return False
    return not stale(release, now, timeout)


def promotion_failure(status_code: int, message: str, branch: str) -> str:
    """What a refused promotion means, in the sentence the release row will carry.

    422 is the one that matters: GitHub's answer to a move that is not a fast-forward, which
    means somebody committed to the release branch directly. The factory never forces it;
    which history wins is a human's call.
    """
    if status_code == 422:
        return (
            f"{branch} diverged: moving it to the integration head is not a fast-forward, so "
            f"something was committed to {branch} directly. Nothing was forced — a human "
            f"decides which history wins. ({message})"
        )
    if status_code == 403:
        return f"the token may not write {branch}: {message}"
    return f"could not move {branch}: {message}"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# --------------------------------------------------------------------------- the work


async def expire_stale(now: dt.datetime | None = None) -> list[int]:
    """Expire every standing release nobody read in time. Returns the ids expired."""
    now = now or _now()
    expired = []
    for release in await db.releases_in(EXPIRABLE):
        if not stale(release, now, settings.release_review_timeout):
            continue
        try:
            await db.transition_release(
                release["id"], "expired",
                error=f"not reviewed within {settings.release_review_timeout}s",
            )
            expired.append(release["id"])
            log.info("release %s (%s) expired unreviewed", release["id"], release["repo"])
        except ValueError:
            continue  # somebody reviewed it in the same instant, which is the better outcome
    return expired


async def recover() -> list[int]:
    """Fail every release left mid-flight by a previous process. Returns the ids failed.

    Called once at startup, when nothing can be in flight yet: a row in `assembling`,
    `deploying` or `accepting` with no task behind it would otherwise freeze its repo forever.
    """
    failed = []
    for release in await db.releases_in(IN_FLIGHT):
        if release["id"] in _inflight:
            continue
        try:
            await db.transition_release(
                release["id"], "failed",
                error=f"interrupted: the control plane restarted while it was {release['status']}",
            )
            failed.append(release["id"])
        except ValueError:
            continue
    return failed


class Frozen(RuntimeError):
    """The repo's latest release still holds the freeze."""


async def assemble(repo: str) -> tuple[dict | None, str]:
    """Start a release of `repo` now, if there is anything to release. Returns (row, why).

    Synchronous up to the new row; the CI wait and the promotion run as a task, because
    waiting for checks can take as long as `FACTORY_MERGE_CHECK_TIMEOUT` and neither the tick
    loop nor an API request should sit inside it. Raises `Frozen` when the repo's latest
    release still holds the freeze — a human asking for a release does not get to lift it by
    asking, only by reviewing.
    """
    latest = await db.latest_release(repo)
    if frozen(latest, _now(), settings.release_review_timeout):
        raise Frozen(f"release {latest['id']} is {latest['status']} and holds {repo} frozen")
    base = await repos.base_branch(repo)
    head = await github.branch_sha(repo, base)
    if head is None:
        return None, f"{repo} has no branch {base}"
    current = await github.branch_sha(repo, settings.release_branch)
    if current == head:
        return None, f"{settings.release_branch} is already at {base} ({head[:8]})"
    candidates = await db.unreleased_items(repo)
    if not candidates:
        return None, f"no merged factory work on {base} since the last release"

    release = await db.create_release(repo, test_sha=head)
    log.info("release %s: %s %s at %s, %s candidate item(s)",
             release["id"], repo, base, head[:8], len(candidates))
    task = asyncio.create_task(_finish(release["id"], repo, head, candidates))
    _inflight[release["id"]] = task
    task.add_done_callback(lambda _t, rid=release["id"]: _inflight.pop(rid, None))
    return release, f"assembling {head[:8]}"


async def _fail(release_id: int, error: str) -> None:
    try:
        await db.transition_release(release_id, "failed", error=error[:1000])
    except ValueError as exc:
        log.warning("release %s could not be failed: %s", release_id, exc)


async def _finish(release_id: int, repo: str, head: str, candidates: list[dict]) -> None:
    """Wait for green CI on the exact commit, then promote it. Owns the row's ending."""
    try:
        green, why, _failed = await github.checks_green(
            repo, head, timeout=settings.merge_check_timeout
        )
        if not green:
            await _fail(release_id, f"CI on {head[:8]} is not green: {why}")
            return
        await promote(release_id, repo, head, candidates)
    except asyncio.CancelledError:
        await _fail(release_id, "cancelled")
        raise
    except Exception as exc:  # noqa: BLE001 - one release failing must not stop the loop
        log.exception("release %s crashed", release_id)
        await _fail(release_id, f"crashed: {str(exc)[:200] or type(exc).__name__}")


async def promote(release_id: int, repo: str, head: str, candidates: list[dict]) -> None:
    """Fast-forward the release branch onto `head`, creating it the first time.

    Items are worked out after the move rather than before, from the range that actually
    moved: a candidate merged after `head` was read is not in it and rides the next release.
    """
    branch = settings.release_branch
    before = await github.branch_sha(repo, branch)
    try:
        if before is None:
            await github.create_branch(repo, branch, head)
        else:
            await github.update_branch(repo, branch, head)
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code if exc.response is not None else 0
        await db.transition_release(
            release_id, "failed", base_sha_before=before,
            error=promotion_failure(status, str(exc), branch),
        )
        return
    items = await _items_in_range(repo, before, head, candidates)
    await db.add_release_items(release_id, items)
    await db.transition_release(
        release_id, "promoted", base_sha_before=before, promoted_at=db.utcnow()
    )
    log.info("release %s: %s %s → %s with %s item(s)",
             release_id, repo, branch, head[:8], len(items))


async def _items_in_range(
    repo: str, before: str | None, head: str, candidates: list[dict]
) -> list[dict]:
    """The candidates whose squash commit is in `before..head`, each with a PR excerpt.

    The release branch has already moved by now, so nothing here may fail the release: a
    compare that errors falls back to every candidate, and a pull request that cannot be read
    is listed without its summary.
    """
    chosen = candidates
    if before is not None:
        try:
            moved = set(await github.commits_between(repo, before, head))
            chosen = [c for c in candidates if c["merged_sha"] in moved]
        except Exception:  # noqa: BLE001 - see the docstring
            log.exception("could not compare %s..%s on %s; listing every candidate",
                          before[:8], head[:8], repo)
    out = []
    for item in chosen:
        summary = None
        try:
            number = int(str(item.get("pr_url") or "").rstrip("/").split("/")[-1])
            pr = await github.pull_request(repo, number)
            summary = " ".join(pr["body"].split())[:SUMMARY_MAX] or None
        except Exception:  # noqa: BLE001 - an excerpt is a convenience
            pass
        out.append({**item, "summary": summary})
    return out


async def mark_reviewed(release_id: int) -> dict:
    """A human read the release. Lifts the freeze. Raises `ValueError` when it cannot move."""
    return await db.transition_release(release_id, "reviewed", reviewed_at=db.utcnow())


# --------------------------------------------------------------------------- the loop


async def _tick() -> None:
    now = _now()
    await expire_stale(now)
    if not settings.release_enabled:
        return
    times = parse_times(settings.release_times)
    for repo in repos.watched():
        try:
            latest = await db.latest_release(repo)
            moment = due_moment(
                now, times, settings.release_tz, latest["updated_at"] if latest else None
            )
            if moment is None or _checked.get(repo) == moment:
                continue
            _checked[repo] = moment
            if frozen(latest, now, settings.release_review_timeout):
                log.info("%s: %s moment skipped, release %s is %s",
                         repo, moment.isoformat(), latest["id"], latest["status"])
                continue
            release, why = await assemble(repo)
            log.info("%s: %s", repo, why if release is None else f"release {release['id']} {why}")
        except Exception:  # noqa: BLE001 - one repo must not stop the others
            log.exception("release tick failed for %s", repo)


async def _loop() -> None:
    log.info("release schedule %s %s, every %ss, %s",
             settings.release_times, settings.release_tz, settings.release_tick,
             "on" if settings.release_enabled else "off (expiry only)")
    while True:
        await _tick()
        await asyncio.sleep(settings.release_tick)


async def start() -> None:
    """Recover interrupted releases, then run the schedule.

    Runs with FACTORY_RELEASE off too, because expiry still applies to releases started by
    hand; it is assembly on the schedule that the switch governs.
    """
    global _task
    if _task is not None or settings.release_tick <= 0:
        return
    failed = await recover()
    if failed:
        log.warning("failed %s release(s) interrupted by a restart: %s", len(failed), failed)
    _task = asyncio.create_task(_loop())


async def stop() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except asyncio.CancelledError:
        pass
    _task = None
