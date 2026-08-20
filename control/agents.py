"""The golden naming contract.

A golden is a boxd snapshot, and its *name* says which repo it was warmed for:

    golden-copy               the base image — tooling and the agent's auth, no repo
    golden-<repo-slug>        the base image with one repo already cloned and installed

    resolution:  golden-<repo-slug>  →  golden-copy  →  nothing

The agent is deliberately not in the name any more. It used to be (`golden-<agent>` and
`golden-<agent>--<repo-slug>`), which made "which agents exist?" a question you answered by
listing snapshots — elegant, and wrong in the direction the platform is going. A deployment
watches many repos and runs one agent; encoding the agent meant every repo's warm tier was
named after the thing that never varies, and the thing that does vary needed a separator
convention to survive. Now the name carries the repo and only the repo.

Which agent runs inside an image is a property of the image, and the image says so itself:
each golden carries `/etc/factory/agent.json`, announced into the run log as
`FACTORY-MANIFEST` and recorded on the run (`runner._stream_agent`). That is the registry,
and it is the one that cannot drift from what actually booted. Running a *second* agent means
a second base image and a way to choose between them; that is on the backlog, not here.

Two tiers, not one, and the fallback is what makes connecting a repo cheap: a repo with no
golden of its own still dispatches onto `golden-copy` and installs for itself. Provisioning is
a speed-up that can finish later, never a gate on the first run.

Everything here is a pure function over strings apart from the four that talk to boxd —
`available()`, `listed()`, `captured_version()` and `wait_until_captured()` — which all go
through the one listing. That is deliberate: dispatch decisions can then be tested without
credentials, a VM, a database or a clock.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Iterable

GOLDEN_PREFIX = "golden-"

# The base image. Named by the owner, so it is a constant here rather than something derived:
# every repo without a warm golden of its own boots this one.
#
# It cannot collide with a per-repo name. A slug is built from `owner/name`, and the `/` alone
# guarantees at least one hyphen, so no repo slug is ever the single word `copy`.
BASE_SNAPSHOT = f"{GOLDEN_PREFIX}copy"

# What `parse_golden` returns for the base image: a golden that carries no repo. Empty rather
# than `None` because `None` already means "not a golden at all", and those are different
# answers — one is the image every run can fall back to, the other is somebody's unrelated
# snapshot. Test with `is None`, never for truthiness.
BASE = ""

# The agent every golden currently runs. Recorded on a run so the ledger says which agent did
# the work, and overwritten by the golden's own manifest when it announces something else.
# It selects nothing: no snapshot name, no dispatch path.
DEFAULT_AGENT = "claude"

# How long a fleet listing is reused. Short on purpose: provisioning creates a snapshot and
# then wants to dispatch onto it moments later, so a stale cache would hide the golden that
# was just built. Ten seconds still collapses the burst of lookups inside a single poll.
CACHE_TTL = 10.0

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slug(repo: str) -> str:
    """`owner/name` to the repo half of a golden name.

    Lowercase, every run of non-alphanumerics collapsed to a single hyphen. The owner stays
    in: two owners with a repo of the same name must not land on the same snapshot.

    Unchanged from the agent-in-the-name design, and it has to stay unchanged —
    `scripts/build-golden.sh` and `scripts/refresh-golden.sh` reimplement it in shell, and
    `control/golden_scripts_test.py` pins the two implementations in agreement. A slug that
    disagrees by one character names a snapshot no dispatch will ever resolve onto.
    """
    return _SLUG_RE.sub("-", (repo or "").lower()).strip("-")


def golden_name(repo: str | None = None) -> str:
    """The snapshot name for `repo`, or the base image when given none.

    Inverse of `parse_golden`. A repo whose slug comes out empty — anything with no
    alphanumerics in it at all — gets the base name, because there is no honest per-repo name
    to build and silently returning `golden-` would name a snapshot nothing can parse.
    """
    repo_slug = slug(repo) if repo else ""
    return f"{GOLDEN_PREFIX}{repo_slug}" if repo_slug else BASE_SNAPSHOT


def parse_golden(name: str) -> str | None:
    """The repo slug a golden name carries, `BASE` for the base image, `None` for a non-golden.

    Strict by design — this is what stops an unrelated snapshot from being read as a golden.
    `goldenrod` is a machine someone named; `golden-` names no repo; `golden--acme` is fringed
    with the hyphens a slug can never begin with.
    """
    name = (name or "").strip()
    if not name.startswith(GOLDEN_PREFIX):
        return None
    if name == BASE_SNAPSHOT:
        return BASE
    repo_slug = name[len(GOLDEN_PREFIX):]
    if not repo_slug or repo_slug.startswith("-") or repo_slug.endswith("-"):
        return None
    return repo_slug


def is_golden(name: str) -> bool:
    """Whether `name` belongs to the factory at all."""
    return parse_golden(name) is not None


def resolve_snapshot(repo: str | None, available: Iterable[str] = ()) -> str | None:
    """Which snapshot a run for `repo` boots: its own warm golden, else the base image.

    `None` means this deployment has no base image, which is a configuration error the caller
    must surface rather than paper over with somebody else's snapshot. A repo that simply has
    not been provisioned yet is *not* that case — it gets the base and installs for itself.
    """
    names = set(available or ())
    warm = golden_name(repo) if repo else ""
    if warm and warm != BASE_SNAPSHOT and warm in names:
        return warm
    return BASE_SNAPSHOT if BASE_SNAPSHOT in names else None


def api_rows(known: dict[str, dict]) -> list[dict]:
    """One row per golden snapshot, for the fleet page.

    `known` is `db.snapshots()` — what the refresh loop last saw, which is the only registry
    there is. Shaped here rather than in the endpoint because it is a pure function over rows
    and a name, and everything about the naming contract is testable without a database, a
    fleet or a clock.

    The base sorts first: an empty per-repo list is explained by the base being missing, and
    never the other way round. Rows whose name is not a golden are dropped — the table is
    keyed on snapshot names and a row that cannot be parsed as one is a row the refresh should
    never have written.
    """
    out = []
    for name, row in (known or {}).items():
        repo_slug = parse_golden(name)
        if repo_slug is None:
            continue
        out.append(
            {
                "snapshot": name,
                "repo": row.get("repo") or repo_slug or None,
                "base": repo_slug == BASE,
                # Which agent this image actually launches, from the manifest it announced on
                # the way into its last run. Read back from the run rather than from the
                # machine, because asking the machine means booting it.
                "agent": row.get("agent") or None,
                "version": row.get("version") or None,
                # `pending` with no version is a capture still being written; `pending` with
                # one is a re-save, and the older version is still bootable. Only the first
                # is a golden nothing can boot, which is why `ready` is not `status ==
                # "ready"`.
                "status": row.get("status") or None,
                "ready": bool(row.get("version")),
                "events": row.get("events") or None,
                "agent_version": row.get("agent_version") or None,
                # From the runs, not from a probe: `ok` is what the last run on this snapshot
                # did, and `verified_at` is when one last finished on it having produced
                # usage. Null `verified_at` is unproven, not broken.
                "ok": bool(row.get("ok")),
                "error": row.get("error") or None,
                "verified_at": row.get("verified_at") or None,
            }
        )
    out.sort(key=lambda r: (not r["base"], r["snapshot"]))
    return out


_cache: tuple[float, tuple[str, ...], tuple[str, ...]] | None = None
_versions: dict[str, str] = {}
_statuses: dict[str, str] = {}


def version(name: str) -> str:
    """The snapshot version the last listing saw for `name`, or `""` if it saw none.

    Kept beside the listing rather than fetched on demand, because it arrives with it: the
    fleet call already carries the version, and asking again would be a second round trip
    for something already in hand. `""` means "the last listing did not include this name",
    which is the same answer as for a snapshot that never existed — the caller has no use
    for the difference.

    It is also how "can this be restored?" is answered — see `available`.
    """
    return _versions.get(name, "")


def status(name: str) -> str:
    """What the last listing said `name` was doing: `ready`, `pending`, or `""` if unseen."""
    return _statuses.get(name, "")


async def _listing(boxd) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """`(every golden, the ones that can actually be restored)`, memoised for `CACHE_TTL`.

    The split is the whole point, and it cost a run to learn. boxd reports one row per
    snapshot *name*: `version` is the newest **ready** capture and `status` is what the name
    is doing right now. So there are two distinct states that both read as "pending":

    - a re-save of a golden that already has a ready version — the older one stays
      restorable, and dropping the name would make a working golden vanish mid-poll;
    - a **first** capture, `version=None` — there is nothing behind it to restore, and boxd
      answers `create(from_snapshot=...)` with `ConflictError: snapshot is 'pending' (no
      ready version yet)`.

    Filtering on `status` alone gets the first case wrong; not filtering at all gets the
    second one wrong, which is what happened the first time a repo's golden was provisioned
    and dispatched onto seconds later: three attempts, no VM, the issue halted. Having a
    version is exactly the question — "is there something here to boot" — so that is what is
    asked.
    """
    global _cache, _versions, _statuses
    now = time.monotonic()
    if _cache and now - _cache[0] < CACHE_TTL:
        return _cache[1], _cache[2]
    found = [s for s in await boxd.snapshots.list() if is_golden(s.name)]
    # Replaced wholesale, never merged: a listing is the whole truth about the fleet at that
    # moment, so a snapshot that has been deleted must leave with it rather than linger as a
    # version nobody can fork any more.
    _versions = {s.name: str(getattr(s, "version", "") or "") for s in found}
    _statuses = {s.name: str(getattr(s, "status", "") or "") for s in found}
    names = tuple(sorted(s.name for s in found))
    ready = tuple(n for n in names if _versions.get(n))
    _cache = (now, names, ready)
    return names, ready


async def available(boxd) -> tuple[str, ...]:
    """Golden snapshots a run could boot right now.

    `boxd` is an `AsyncBoxd`, left untyped so this module imports no third-party package and
    the rest of it can be exercised without one.

    This is the dispatch answer, so a capture with no ready version behind it is not in it —
    see `_listing`. Preflight and `settings.problems` ask the same question for the same
    reason: a golden that cannot be restored yet is one a run cannot use yet, and reporting
    it as present is how a repo gets dispatched into a `ConflictError`.
    """
    return (await _listing(boxd))[1]


async def listed(boxd) -> tuple[str, ...]:
    """Every golden the fleet holds, restorable or not.

    What the fleet view shows, and what the refresh loop records. A snapshot still capturing
    is a thing the operator built and is waiting on; hiding it until it goes ready would make
    the Goldens page answer "where did it go?" for the two minutes it matters most.
    """
    return (await _listing(boxd))[0]


def forget() -> None:
    """Drop the memoised listing, so the next lookup re-reads the fleet."""
    global _cache, _versions, _statuses
    _cache = None
    _versions = {}
    _statuses = {}


async def captured_version(boxd, name: str) -> str:
    """The restorable version `name` carries right now, re-read from the fleet.

    Forces a fresh listing rather than trusting the memoised one, because every caller is
    asking about a moment they are about to change: what was there *before* a capture, and
    whether the capture has landed since.
    """
    forget()
    await listed(boxd)
    return version(name)


async def wait_until_captured(
    boxd, name: str, was: str, timeout: float, poll: float = 5.0
) -> str:
    """Block until `name` carries a restorable version newer than `was`. Returns that version.

    `boxd.snapshots.create` returns when the capture has been *queued*, not when it has been
    written — and destroying the machine underneath a queued capture aborts it for good. That
    is not a hypothesis: `golden-mithril-studio-software-factory` sat at `v0 / pending / 0B`
    with no machine behind it, unrestorable and unfixable, because `provision._execute`
    reaped the VM the instant `create` returned. `scripts/build-golden.sh` never hit it
    because the `boxd snapshots save` CLI blocks until the capture lands.

    Compared against `was` rather than merely tested for existence, because a *rebuild*
    re-saves a name that already has a ready version from last time: "has a version" is true
    before the new capture starts, and waiting on it would return immediately and reap the
    machine exactly as before. The question is whether the version moved.

    `TimeoutError` is the caller's cue to delete the half-written snapshot — a name with no
    version behind it is one no dispatch can ever resolve onto, and leaving it in the fleet
    turns a slow provision into a permanent one.
    """
    deadline = time.monotonic() + timeout
    while True:
        found = await captured_version(boxd, name)
        if found and found != was:
            return found
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"{name} was still {status(name) or 'unlisted'} after {timeout:.0f}s"
                + (f" (version {was!r} unchanged)" if was else " with no version behind it")
            )
        await asyncio.sleep(poll)
