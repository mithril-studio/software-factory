"""Discovery: find the next `agent:queued` issue per watched repo and start its run.

One run per repo at a time, lowest issue number first. With FACTORY_SEQUENTIAL on (the
default) a repo also waits while a factory pull request into its target branch is still open,
so the next issue branches from a target that already contains the previous one.
"""

from __future__ import annotations

import asyncio
import logging

from . import db, github, repos, runner
from .config import settings

log = logging.getLogger("factory.poller")

_task: asyncio.Task | None = None
_labelled: set[str] = set()


async def _ensure_labels_once(repo: str) -> None:
    if repo in _labelled:
        return
    await github.ensure_labels(repo)
    _labelled.add(repo)


async def next_issue(repo: str) -> int | None:
    """The issue the factory should start on `repo` now, or None."""
    if await db.has_active_run(repo):
        return None
    issues = await github.list_issues_with_label(repo, github.LABEL_QUEUED)
    if not issues:
        return None
    number = issues[0]["number"]
    if settings.sequential:
        heads = await github.open_pr_heads(repo, repos.target_branch(repo), prefix="factory/")
        # Re-queuing an issue to address feedback on its own open PR is always allowed.
        waiting = sorted(h for h in heads if h != f"factory/issue-{number}")
        if waiting:
            log.info("%s: #%s waits for %s to be merged or closed", repo, number, ", ".join(waiting))
            return None
    return number


async def _poll_repo(repo: str) -> None:
    await _ensure_labels_once(repo)
    number = await next_issue(repo)
    if number is None:
        return
    log.info("dispatching %s#%s", repo, number)
    try:
        await runner.create(repo, number)
    except runner.Busy:
        pass


async def _tick() -> None:
    for repo in repos.watched():
        try:
            await _poll_repo(repo)
        except Exception:  # noqa: BLE001 - one bad repo must not stop the loop
            log.exception("poll failed for %s", repo)


async def _loop() -> None:
    log.info("polling every %ss", settings.poll_interval)
    while True:
        await _tick()
        await asyncio.sleep(settings.poll_interval)


def start() -> None:
    global _task
    if _task is not None or not settings.poll_enabled:
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
