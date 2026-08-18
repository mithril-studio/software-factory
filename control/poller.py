"""Watch configured repos for queued issues and dispatch them.

This is discovery only — it decides *what to pick up next*, nothing about the work itself.
The runner owns the whole run lifecycle and mirrors state back onto the issue; this loop
just finds the next issue and hands it over.

The contract with whoever files work (a human today, a composer later) is a single label:
drop `agent:queued` on an open issue and the factory builds it. Issues are claimed lowest
number first, and a repo runs one issue at a time — so a numbered sequence executes in
order without any dependency graph. Postgres, not the issue label, is the source of truth
for what is already running (`db.has_active_run`), so a slow label write can never cause a
double dispatch.
"""

from __future__ import annotations

import asyncio
import logging

from . import db, github, runner
from .config import settings

log = logging.getLogger("factory.poller")

_task: asyncio.Task | None = None


async def _poll_repo(repo: str) -> None:
    # One run per repo at a time. This is what makes a numbered issue list sequential:
    # #2 does not start until #1 has reached a terminal state (its retries included).
    if await db.has_active_run(repo):
        return
    # A permanently-failed issue (out of retries, labelled agent:failed) blocks the ones
    # after it in a sequential project. Stop dispatching this repo until a human clears it.
    if settings.halt_on_failure:
        failed = await github.list_issues_with_label(repo, github.LABEL_FAILED)
        if failed:
            log.info("%s halted: issue #%s failed, needs a human", repo, failed[0]["number"])
            return
    issues = await github.list_issues_with_label(repo, github.LABEL_QUEUED)
    if not issues:
        return
    issue = issues[0]  # lowest number
    agent = settings.agent_for(repo)
    log.info("dispatching %s#%s to %s", repo, issue["number"], agent)
    await runner.create(repo, issue["number"], agent=agent)


async def _tick() -> None:
    for repo in settings.repos:
        try:
            await _poll_repo(repo)
        except Exception:  # noqa: BLE001 - one bad repo must not stop the loop
            log.exception("poll failed for %s", repo)


async def _loop() -> None:
    # Create the lifecycle labels once so the first dispatch's label writes can't fail on a
    # repo that has never seen the factory. Best-effort: a repo we can't touch yet is skipped.
    for repo in settings.repos:
        try:
            await github.ensure_labels(repo)
        except Exception:  # noqa: BLE001
            log.exception("could not ensure labels on %s", repo)

    log.info("polling %s every %ss", ", ".join(settings.repos), settings.poll_interval)
    while True:
        await _tick()
        await asyncio.sleep(settings.poll_interval)


def start() -> None:
    """Launch the poll loop, unless polling is off or no repos are configured."""
    global _task
    if _task is not None:
        return
    if not settings.poll_enabled or not settings.repos:
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
