"""Which goldens this deployment can boot, and how far any of them can be trusted.

This used to be a freshness sweep: hourly, it forked nothing but `exec`d into every golden
*machine* and asked how far behind its checkout was, whether the tree was dirty, whether a
dependency manifest had moved. A golden is a snapshot now, so there is no machine to ask —
and staleness stopped being the interesting question anyway, because a warm checkout is only
a speed-up and a stale one is re-provisioned rather than diagnosed. The failure that actually
kills goldens was never on that list.

What kills a golden is credential expiry. The only real test of a credential is using it,
and the runs are already doing that, on the exact machine the question is about, for free.
So this grades by evidence: `verified_at` is when a run last finished on a snapshot having
produced usage, which is proof that its `claude` login and its `gh` token both still work.
Nothing here boots a VM, and nothing here repairs anything.

What is left costs one list call, which is why it runs every five minutes rather than every
hour: noticing a new golden late is the cost that replaced discovering staleness late, and a
snapshot — built by hand, or by the provisioning run for a repo somebody just connected —
should be dispatchable within a poll or two of existing.
"""

from __future__ import annotations

import asyncio
import json
import logging

from . import agents, db, runner
from .config import settings

log = logging.getLogger("factory.goldens")

_task: asyncio.Task | None = None


def _manifest(raw: str | None) -> dict:
    """The manifest a run recorded, or `{}`. Never raises — it is stored input, not ours."""
    try:
        parsed = json.loads(raw or "")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def refresh() -> dict[str, dict]:
    """List the golden snapshots and record one row each. Returns the rows it wrote.

    Never raises: a boxd outage is a refresh that recorded nothing, not a control plane that
    fell over. It writes one row per *discovered* name and no rows for anything else, so a
    snapshot that has been deleted keeps its last row rather than being resurrected with an
    empty one — the fleet listing, not this table, is what `available()` answers from.
    """
    boxd = runner.client()
    try:
        # The refresh is the one caller that must not read a memoised fleet: it exists to
        # notice change, and a listing up to `CACHE_TTL` old is exactly what it is looking for.
        agents.forget()
        # Everything the fleet holds, not just what a run could boot: a snapshot still being
        # captured is a thing somebody built and is waiting on, and it should be visible while
        # it is pending rather than appearing only once it goes ready.
        names = await agents.listed(boxd)
    except Exception:  # noqa: BLE001 - a fleet nobody can list is nothing to record
        log.exception("could not list the golden snapshots")
        return {}
    finally:
        await boxd.close()

    evidence = await db.snapshot_evidence()
    checked_at = db.utcnow()
    rows: dict[str, dict] = {}
    for name in names:
        seen = evidence.get(name, {})
        manifest = _manifest(seen.get("manifest"))
        rows[name] = {
            # The repo this golden was warmed for, straight off the name; NULL for the base.
            "repo": agents.parse_golden(name) or None,
            # Which agent the image launches — what it announced about itself, not what its
            # name implies. The name stopped implying anything when goldens became per-repo.
            "agent": manifest.get("agent"),
            "version": agents.version(name),
            # What the fleet said the snapshot was doing. A `pending` with no version behind it
            # cannot be restored at all — see `agents._listing` — and the page says so rather
            # than showing a golden that would fail at the first dispatch.
            "status": agents.status(name),
            # What the golden announced on the way into its last run. Read back from the
            # run rather than from the machine, because asking the machine means booting it.
            "events": manifest.get("events"),
            "transcript": manifest.get("transcript"),
            "manifest": seen.get("manifest"),
            "agent_version": manifest.get("version"),
            # `ok=0` with no error is a golden nothing has run on yet: unproven, not broken.
            "ok": seen.get("ok", 0),
            "error": seen.get("error"),
            "last_run": seen.get("last_run"),
            "verified_at": seen.get("verified_at"),
            "checked_at": checked_at,
        }
        await db.record_snapshot(name, **rows[name])

    unproven = [n for n, r in rows.items() if not r["verified_at"]]
    log.info("%s golden snapshot(s): %s", len(rows), ", ".join(sorted(rows)) or "none")
    if unproven:
        log.warning("no run has yet proved: %s", ", ".join(sorted(unproven)))
    return rows


async def _loop() -> None:
    log.info("refreshing agents every %ss", settings.agent_refresh_interval)
    while True:
        await refresh()
        await asyncio.sleep(settings.agent_refresh_interval)


def start() -> None:
    """Launch the refresh loop, unless it is switched off.

    No longer conditional on a repo being watched: which goldens exist is a fact about the
    fleet, and the UI asks it of a deployment that watches nothing yet.
    """
    global _task
    if _task is not None or not settings.agent_refresh_interval:
        return
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
