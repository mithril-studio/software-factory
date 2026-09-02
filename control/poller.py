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

When the queue is dry the loop no longer necessarily stops. First the goal-file sync
(`plan.sync_goal_file`) checks whether `.factory/goal.md` changed in the repo — the commit
that arms a goal, or re-arms a met or stalled one. Then a repo with an active goal gets a
plan run, which files the next feature's issues toward that goal or declares it met. Both
hooks live at the bottom of `_poll_repo`, after every guard above them, and are no-ops
unless the deployment opted in.
"""

from __future__ import annotations

import asyncio
import logging

from telemetry import store as telemetry

from . import db, github, plan, repos, runner
from .config import settings

log = logging.getLogger("factory.poller")

# Repos already reported as over budget today, so the log says it once rather than on every
# tick for the rest of the day. Cleared when the day rolls over.
_over_budget: dict[str, str] = {}


async def _within_budget(repo: str) -> bool:
    """Whether `repo`'s autonomous loops may still start work today.

    The ceiling that bounds a *loop*, and the reason the per-run one does not: the planner
    files issues, those issues build for well under the per-run ceiling each, the queue
    drains, and it plans again. Nothing in that cycle is individually expensive and nothing
    in it terminates except an agent judging the goal met.

    **Gates the loops, never the queue.** An issue with `agent:queued` on it is work somebody
    asked for, and refusing to build it is the ceiling doing something nobody wanted: the
    backlog stops halfway, the repo looks broken, and the operator switches the ceiling off —
    which is worse than not having one. The planning and learning runs are what spend without
    being asked, so they are what a spend ceiling has any business stopping.

    That still brakes the cycle rather than only its first step, because the sum below counts
    every kind. Builds the loops caused land on the day's total, and once the total is past
    the ceiling nothing further is dispatched unasked — the queue finishes and stops growing.

    A run started by hand through the API is never checked at all. That is a human overriding
    the cadence, the same way a hand-started build overrides the queue.
    """
    if not settings.max_repo_daily_cost:
        return True
    day = db.utcnow()[:10]
    try:
        spent = await telemetry.spend_since(repo, f"{day}T00:00:00+00:00")
    except Exception:  # noqa: BLE001 - a ceiling that cannot read spend must not halt work
        log.exception("could not read today's spend for %s; allowing dispatch", repo)
        return True
    if spent <= settings.max_repo_daily_cost:
        _over_budget.pop(repo, None)
        return True
    if _over_budget.get(repo) != day:
        _over_budget[repo] = day
        log.warning(
            "%s has spent $%.2f today, over the $%.2f ceiling; no further dispatches "
            "until UTC midnight", repo, spent, settings.max_repo_daily_cost,
        )
    return False

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
        #
        # The daily ceiling gates this branch and only this branch. An issue somebody
        # labelled is work that was asked for, and a factory that stops building what you
        # queued because a planner had an expensive morning is a factory you turn the
        # ceiling off in. What the ceiling is for is the loops: they are what dispatch
        # without being asked, and they are the only things here that can run away.
        #
        # It still brakes the whole cycle, because the ceiling *counts* every kind. A plan
        # run files issues, those issues build, that build spend lands on the day's total —
        # and once the total is past the ceiling no further plan or learning run starts. The
        # queue drains and stops growing, rather than being frozen halfway through.
        # Before the budget gate, not behind it: syncing the goal file is one GitHub GET
        # (throttled inside) and spends no model tokens, and it must run even for repos the
        # ceiling has stopped and for goals in `met` or `stalled` — this is the only seam
        # where a commit to `.factory/goal.md` can arm or re-arm a goal.
        await plan.sync_goal_file(repo)
        if not await _within_budget(repo):
            return
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
