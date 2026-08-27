"""Watch configured repos for queued issues and dispatch them.

This is discovery only — it decides *what to pick up next*, nothing about the work itself.
The runner owns the whole run lifecycle and mirrors state back onto the issue; this loop
just finds the next issue and hands it over.

Which repos it watches comes from `repos.watched()`, not from configuration, so a repo
connected through the API is picked up on the next tick without a restart. The loop runs even
when nothing is watched yet, for the same reason: the list it reads can change under it.

The contract with whoever files work (a human, the factory-compose skill, or a plan run —
see `control/plan.py`) is a single label: drop `agent:queued` on an open issue and the
factory builds it. Issues are claimed lowest number first, and a repo runs one issue at a
time — so a numbered sequence executes in order without any dependency graph. The database,
not the issue label, is the source of truth for what is already running
(`db.has_active_run`), so a slow label write can never cause a double dispatch.

When the queue is dry the loop no longer necessarily stops: a repo with an active goal gets
a plan run, which files the next issues toward that goal or declares it met. That hook
lives at the bottom of `_poll_repo`, after every guard above it, and is a no-op unless the
deployment opted in.
"""

from __future__ import annotations

import asyncio
import logging

from . import db, github, plan, repos, runner
from .config import settings

log = logging.getLogger("factory.poller")

_task: asyncio.Task | None = None

# Repos whose lifecycle labels have been created. Kept because a repo can now be connected
# while the loop is running, so this cannot happen once at startup — and because
# `ensure_labels` is five POSTs, which is fine once per repo and wasteful every thirty
# seconds forever. Idempotent either way; this is about the request budget, not correctness.
_labelled: set[str] = set()


async def _ensure_labels_once(repo: str) -> None:
    """Create the lifecycle labels the first time this process sees `repo`.

    Best-effort: a repo we cannot touch yet is skipped and retried on the next tick, rather
    than stopping the poll. The label writes a dispatch does would fail on a repo that has
    never seen the factory, which is what this prevents.
    """
    if repo in _labelled:
        return
    await github.ensure_labels(repo)
    _labelled.add(repo)


async def _poll_repo(repo: str) -> None:
    await _ensure_labels_once(repo)
    # One run per repo at a time. This is what makes a numbered issue list sequential:
    # #2 does not start until #1 has reached a terminal state (its retries included).
    if await db.has_active_run(repo):
        return
    # An issue that stopped for a human blocks the ones after it in a sequential project.
    # Both states mean that: agent:failed is out of retries, agent:blocked is a review that
    # would not pass. Either way the work is not on the base branch, so the next issue would
    # branch from a base that is missing it — #48 did exactly that, building on a main without
    # #47's memory store, and had to reconstruct it by hand to have anything to validate.
    # Stop dispatching this repo until a human clears whichever it is.
    if settings.halt_on_failure:
        for label in (github.LABEL_FAILED, github.LABEL_BLOCKED):
            stuck = await github.list_issues_with_label(repo, label)
            if stuck:
                log.info(
                    "%s halted: issue #%s is %s, needs a human", repo, stuck[0]["number"], label
                )
                return
    issues = await github.list_issues_with_label(repo, github.LABEL_QUEUED)
    if not issues:
        # The queue is dry — the seam both idle-time loops hang off, each a no-op unless its
        # switch is on and its own conditions hold. The goal loop first: a plan run files the
        # next work (`plan.should_plan`), and sits after the active-run and halt checks above
        # so it can never overlap other work and never fires on a halted repo. Learning may
        # still start beside it — a learning run reads finished work, claims no issue, and
        # deliberately does not count as "busy" (UNCLAIMED_KINDS), so the two do not race.
        await plan.maybe_plan(repo)
        # Nothing queued is the only moment a learning run may start. It is not urgent — the
        # evidence it reads is finished work and will still be there in half an hour — and
        # taking a concurrency slot ahead of an issue somebody is waiting on would make the
        # loop compete with the work it exists to improve.
        await _maybe_learn(repo)
        return
    issue = issues[0]  # lowest number
    log.info("dispatching %s#%s", repo, issue["number"])
    await runner.create(repo, issue["number"])


async def _maybe_learn(repo: str) -> None:
    """Start a learning run if this repo has finished enough issues since its last one.

    Volume rather than a clock, because evidence is what a learning run consumes: a repo that
    has shipped nothing has produced nothing new to read, and a second pass over the same
    window costs a VM to reach the same conclusions — or different ones, from noise.
    """
    if not settings.learn_enabled:
        return
    finished = await db.issues_since_last_learn(repo)
    if finished < settings.learn_every:
        return
    log.info("%s: %s issues since the last learning run, dispatching one", repo, finished)
    await runner.create_learn(repo)


async def _tick() -> None:
    for repo in repos.watched():
        try:
            await _poll_repo(repo)
        except Exception:  # noqa: BLE001 - one bad repo must not stop the loop
            log.exception("poll failed for %s", repo)


async def _loop() -> None:
    log.info("polling every %ss: %s", settings.poll_interval, ", ".join(repos.watched()) or "nothing yet")
    while True:
        await _tick()
        await asyncio.sleep(settings.poll_interval)


def start() -> None:
    """Launch the poll loop, unless polling is switched off.

    Not conditional on anything being watched. It used to be, which meant a deployment that
    booted with an empty register never polled — and connecting a repo through the API would
    have needed a restart to take effect, which is the whole thing this was supposed to stop
    needing. An empty tick costs nothing.
    """
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
