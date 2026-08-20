"""Which repos this deployment watches — a register, not a setting.

Connecting a repo used to mean editing `FACTORY_REPOS` in `.env` on the box and restarting
systemd. That is not something a web interface can do, and it made "add a repo" an operation
that required shell access to a server. The register lives in the database now, and
`FACTORY_REPOS` is what seeds it the first time a deployment boots.

## Why there is a cache at all

`settings.repos` was a frozen dataclass field read synchronously by the poller, the projects
page, the plan page and preflight. Making all of those `await db.list_repos()` is a
cross-cutting rewrite of code that has nothing to do with this change, and it would put a
database round trip inside the poll loop's hot path.

So the database is the truth and this module holds the last thing it said. Every write goes
through here and reloads, which is the only way the two can disagree — a control plane that
mutated the table behind this module's back would serve a stale list until the next write.
There is one process, so that is a rule to keep rather than a race to defend against.

## What `watched()` does not decide

Nothing here says whether a repo can *run*. A repo is dispatchable the moment it is
registered: with no golden of its own it boots `golden-copy` and installs for itself. The
`golden` and `provision_status` columns record what provisioning did, and no dispatch reads
them. That is deliberate — a repo that had to wait for a snapshot before its first run would
make connecting one a minutes-long operation with a failure mode.
"""

from __future__ import annotations

import logging
import re

from . import db
from .config import settings

log = logging.getLogger("factory.repos")

# `owner/name`, the way GitHub spells it. Deliberately strict: the whole point of validating
# here is that a typo becomes a 400 with a message rather than a watched repo that 404s once a
# poll, forever, in a log nobody reads.
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+$")

_cache: tuple[str, ...] = ()
_rows: dict[str, dict] = {}


def valid(repo: str) -> bool:
    """Whether `repo` is shaped like `owner/name`. Says nothing about it existing."""
    return bool(REPO_RE.match((repo or "").strip()))


def watched() -> tuple[str, ...]:
    """The watched repos, in the order they were added. Synchronous, and never raises.

    Empty before `load()` has run, which is the honest answer during startup: the poller's
    first tick happens after the lifespan has seeded and loaded, and every HTTP request
    happens after that too.
    """
    return _cache


def rows() -> list[dict]:
    """The full register row per watched repo — golden, provision status, when it was added."""
    return [_rows[r] for r in _cache]


async def load() -> tuple[str, ...]:
    """Re-read the register from the database into the cache. Returns the new list."""
    global _cache, _rows
    found = await db.list_repos()
    _rows = {r["repo"]: r for r in found}
    _cache = tuple(_rows)
    return _cache


async def seed() -> None:
    """Bring `FACTORY_REPOS` into the register, then load it.

    Additive and idempotent: `add_repo` ignores a repo that is already there, so a deployment
    that has since connected repos through the API does not have them clobbered by an `.env`
    that predates them — and one that has never used the API keeps working with no migration
    step at all.

    Removing a repo from `FACTORY_REPOS` does *not* unwatch it. Deleting is an explicit act
    (`DELETE /api/repos/...`), because the alternative is a deployment silently forgetting a
    connected repo the first time somebody tidies an environment variable.
    """
    for repo in settings.repos:
        if not valid(repo):
            log.warning("FACTORY_REPOS entry %r is not owner/repo; skipped", repo)
            continue
        await db.add_repo(repo)
    await load()
    log.info("watching %s repo(s): %s", len(_cache), ", ".join(_cache) or "none")


async def add(repo: str, agent: str | None = None) -> None:
    """Watch `repo` from now on. Caller validates; this stores and reloads."""
    await db.add_repo(repo.strip(), agent)
    await load()


async def remove(repo: str) -> bool:
    """Stop watching `repo`. Returns whether it was being watched."""
    removed = await db.remove_repo(repo.strip())
    await load()
    return removed


async def record_golden(repo: str, golden: str | None, status: str) -> None:
    """Record what provisioning did for `repo`, and refresh the cached row."""
    await db.set_repo_golden(repo.strip(), golden, status)
    await load()
