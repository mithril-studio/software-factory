"""The repo register: which repos this factory watches, and which branch their PRs target.

Repos live in the database and are connected through the UI (`POST /api/repos`).
`FACTORY_REPOS` only seeds the register on boot; taking an entry out does not unwatch it.
"""

from __future__ import annotations

import logging
import re

from . import db, github
from .config import settings

log = logging.getLogger("factory.repos")

# `owner/name`, the way GitHub spells it.
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+$")

# A branch name GitHub would accept, narrowly.
BRANCH_RE = re.compile(r"^(?!.*\.\.)(?!/)(?!.*//)[A-Za-z0-9._/-]+(?<![./])$")

_cache: tuple[str, ...] = ()
_rows: dict[str, dict] = {}


def valid(repo: str) -> bool:
    """Whether `repo` is shaped like `owner/name`. Says nothing about it existing."""
    return bool(REPO_RE.match((repo or "").strip()))


def watched() -> tuple[str, ...]:
    """The watched repos, in the order they were added. Synchronous, never raises."""
    return _cache


def rows() -> list[dict]:
    return [_rows[r] for r in _cache]


def row(repo: str) -> dict | None:
    return _rows.get(repo)


async def load() -> tuple[str, ...]:
    """Re-read the register from the database into the cache."""
    global _cache, _rows
    found = await db.list_repos()
    _rows = {r["repo"]: r for r in found}
    _cache = tuple(_rows)
    return _cache


async def seed() -> None:
    """Fill an empty register from `FACTORY_REPOS`, then load it.

    Only an empty one. Once a repo is in the register the Projects page owns the list, and a
    seed applied on every boot re-added each repo a human had disconnected: two deploys on
    2026-11-12 put foundation-e-learning and specter-ai back on the watch list.
    """
    if not await db.list_repos():
        for repo in settings.repos:
            if not valid(repo):
                log.warning("FACTORY_REPOS entry %r is not owner/repo; skipped", repo)
                continue
            await db.add_repo(repo)
    await load()
    log.info("watching %s repo(s): %s", len(_cache), ", ".join(_cache) or "none")


async def add(repo: str) -> None:
    await db.add_repo(repo.strip())
    await load()


async def remove(repo: str) -> bool:
    removed = await db.remove_repo(repo.strip())
    await load()
    return removed


def target_branch(repo: str) -> str:
    """The branch `repo`'s pull requests go into: its own setting, else FACTORY_TARGET_BRANCH."""
    return (_rows.get(repo) or {}).get("base_branch") or settings.target_branch


async def ensure_target(repo: str) -> str:
    """The target branch for `repo`, created from the default branch if it does not exist."""
    target = target_branch(repo)
    if await github.branch_sha(repo, target) is not None:
        return target
    default = await github.default_branch(repo)
    sha = await github.branch_sha(repo, default)
    if sha is None:
        raise RuntimeError(f"{repo} has no commits on {default}; nothing to branch {target} from")
    await github.create_branch(repo, target, sha)
    log.info("created %s on %s from %s", target, repo, default)
    return target


async def set_target_branch(repo: str, branch: str | None) -> dict:
    """Point `repo` at another target branch, or back at the default with None or ""."""
    repo = repo.strip()
    if repo not in _rows:
        raise ValueError(f"{repo} is not watched")
    name = (branch or "").strip() or None
    if name is not None and not BRANCH_RE.match(name):
        raise ValueError(f"{name!r} is not a branch name")
    await db.set_repo_base_branch(repo, name)
    await load()
    return _rows[repo]
