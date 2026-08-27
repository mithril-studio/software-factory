"""The goal loop: plan the next increment of a repo's backlog when its queue runs dry.

A watched repo can carry a **goal** — prose describing the endstate of the project, written
by a human through the API and stored on the register row. When the poller finds a repo with
an active goal, no queued issues, no run in flight and its cooldown elapsed, it dispatches a
run of this kind: an agent on a fresh VM that reads the repository as it actually is,
compares it against the goal, and either files the next batch of `agent:queued` issues (in
the factory-compose format, so each carries executable acceptance criteria) or declares the
goal met. The existing pipeline then builds what was filed, and when the queue runs dry
again the loop plans again — until a plan run finds nothing left to do.

## Why planning happens in a VM

`control/` contains no model calls, and deciding what to build next is the most
judgement-heavy step in the whole pipeline — so it runs where every other judgement runs: an
agent on a machine restored from the repo's golden, with the checkout in front of it. The
control plane's half is everything deterministic: when to plan, how to read the verdict
back, and what the repo's goal state becomes. Those three are `should_plan`,
`parse_plan_verdict` and `plan_outcome` below, pure functions for the same reason
`runner.decide` is one.

## The verdict is verified, not trusted

The planner reports `{"goal_met": ..., "issues_created": [...], "summary": ...}` in
/tmp/factory-plan.json, but what moves the goal state is what GitHub actually shows: a repo
only reaches `met` when the verdict says so *and* no `agent:queued` issue exists, and
"issues created" only counts if the queue is really non-empty afterwards. Everything fails
toward doing work or stopping for a human, never toward declaring a project finished on an
agent's say-so.

## What stops a runaway planner

Four fences, one per failure mode. `FACTORY_PLAN` off means no planning at all — filing
issues is autonomous spend, so it is opt-in. `FACTORY_PLAN_COOLDOWN` bounds how often one
repo may plan, stamped at dispatch so crashes count. `FACTORY_PLAN_MAX_STALLS` consecutive
fruitless plans (crashed, no verdict, or claimed issues that never appeared) mark the goal
`stalled` and hand it to a human. And `FACTORY_PLAN_MAX_ISSUES` caps one pass's output —
an instruction the prompt carries and the control plane observes, since an issue cannot be
un-created. Halt-on-failure needs no fence here: the poller checks it before it ever reaches
the empty-queue branch this module hangs off.

## Modelled as a run

A `runs` row with `kind = 'plan'`, the same reasoning as provisioning: it is a run on a VM
restored from a golden and it wants the streamed log, the cancel button, the reaper and the
runs table. Unlike provisioning it runs a real agent, so it takes the build semaphore (it
spends tokens) and reuses the runner's dispatch machinery wholesale. `issue_number` is 0 —
there is no issue yet; making them is the job.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import uuid
from pathlib import Path

from . import agents, db, github, repos, runner
from .config import settings

log = logging.getLogger("factory.plan")

PLAN_VERDICT_PATH = "/tmp/factory-plan.json"

# Written into a fruitless plan run's `error` column, with the summary after it — the same
# shape as runner's REVIEW_* tags, and for the same reader: a run whose status says
# `succeeded` still has to say what it decided.
PLAN_FRUITLESS = "planned nothing: "

# The planner works on the base branch and changes nothing. No NODE_GUARD: the planner reads
# and files issues, and a toolchain mismatch that matters to a build is not a reason to
# refuse to plan one. Same launch block as the other agent scripts — POSIX, /bin/sh.
PLAN_SCRIPT = runner.PRELUDE + r"""
rm -f /tmp/factory-plan.json
echo "FACTORY: checking out $FACTORY_BASE for planning"
git checkout -B "$FACTORY_BASE" "origin/$FACTORY_BASE" || { echo "FACTORY: checkout failed" >&2; exit 92; }
echo "FACTORY: starting planner"
command -v factory-agent >/dev/null 2>&1 || { claude -p "$FACTORY_PROMPT" \
  --effort "$FACTORY_AGENT_EFFORT" \
  --disallowed-tools Agent Task ScheduleWakeup \
  --dangerously-skip-permissions \
  --output-format stream-json --verbose < /dev/null; exit $?; }
echo "FACTORY-MANIFEST $(tr -d '\n' < /etc/factory/agent.json 2>/dev/null)"
exec factory-agent
"""


PLAN_PROMPT_TEMPLATE = """You are the planner for an autonomous software factory, working \
alone in a VM on a checkout of {repo} at its default branch (`{base}`). The factory builds
GitHub issues labelled `agent:queued`, lowest number first, one at a time, and reviews each
against its acceptance criteria before merging. The queue is empty. Your job is to compare
the repository as it exists against its goal, and either declare the goal met or create the
next batch of issues.

--- the goal ---
{goal}
--- end goal ---

How to work:
1. Learn the current state. Read the code, the README, `.factory.md` and `.mem/` if they
   exist. Then read the history of work: `gh issue list --repo {repo} --state closed
   --limit 200` for what has shipped, and `gh issue list --repo {repo} --state open` for
   what is already filed. An open issue that covers work you would plan means you do not
   file it again.
2. Judge the gap. For each part of the goal, find evidence in the repository that it is
   satisfied — a file you read, a command you ran, a feature that demonstrably works. Not a
   hope: something you observed.
3. If every part of the goal is satisfied, create nothing. Write the verdict (step 5) with
   "goal_met": true and cite the evidence in "summary".
4. Otherwise plan ONLY the next increment: at most {max_issues} issues that move the project
   one coherent step toward the goal. Not the whole remaining backlog — the factory calls
   you again when these are built, and a plan written against today's code is better than a
   stale one written all at once. Order them by dependency and create them in that order:
   they are built lowest issue number first, so an issue may assume everything filed before
   it is already merged. Each issue must be independently buildable and verifiable by an
   agent with no context beyond the issue text, and must carry these sections:
   - `## Task` — what to build and why, self-contained.
   - `## Where this goes` — the files/directories the work is expected to land in (advisory).
   - `## Boundaries` — including a `Never:` lane for what the builder must not touch.
   - `## Acceptance criteria` — a fenced ```yaml list of entries shaped
     `{{id, mode, statement, verify}}`, modes `test` | `probe` | `structure` | `inspect`.
     Each `verify` must be executable in a fresh checkout — a test file path for `test`, a
     shell command for `probe` and `structure`, a path to read for `inspect`. A reviewing
     agent runs every one of these before the change merges, so a criterion that cannot be
     executed is a criterion that blocks the issue forever.
   If the `factory-compose` skill is installed, load it and follow its issue template
   exactly (skip its interactive approval flow — there is nobody here to approve).
   Create each issue with:
     gh issue create --repo {repo} --title "<title>" --body-file <file> --label "agent:queued"
   and record the issue number each create prints.
5. Your final act: write /tmp/factory-plan.json and nothing after it:
   {{"goal_met": true|false, "issues_created": [<numbers in creation order>],
     "summary": "<what you found, what you planned, or the evidence the goal is met>"}}

Hard rules:
- Do not modify the repository. No commits, no pushes, no branches, no pull requests, no
  editing or closing existing issues. Creating new issues and writing the verdict file is
  the entirety of your output.
- At most {max_issues} issues. Fewer is better; the factory loops.
- Run long commands in the foreground with an explicit timeout, e.g. `timeout 600 <cmd>`.
  Background tasks never deliver notifications here; a run that waits for one dies silent.

This project, as it describes itself:

{project_notes}
"""


# --------------------------------------------------------------------------- pure decisions


def cooldown_elapsed(last_planned_at: str | None, now: str, cooldown: int) -> bool:
    """Whether enough time has passed since the last plan dispatch.

    An unparseable stamp counts as elapsed: both timestamps are written by `db.utcnow`, so a
    bad one means somebody edited the table by hand, and the loop is still bounded by
    `has_active_run` and by this stamp being rewritten on the very next dispatch.
    """
    if not last_planned_at:
        return True
    try:
        last = dt.datetime.fromisoformat(last_planned_at)
        current = dt.datetime.fromisoformat(now)
    except ValueError:
        return True
    return (current - last).total_seconds() >= cooldown


def should_plan(row: dict | None, now: str, enabled: bool, cooldown: int) -> bool:
    """Whether a repo whose queue just came up empty should get a plan run.

    The caller has already established the expensive facts — no active run, no halt label,
    no queued issue — because this hangs off the poller's empty-queue branch, downstream of
    all three checks. What is decided here is only the goal loop's own policy: switched on,
    a goal that is present and `active`, and the cooldown elapsed.
    """
    if not enabled or row is None:
        return False
    if row.get("goal_state") != repos.GOAL_ACTIVE:
        return False
    if not (row.get("goal") or "").strip():
        return False
    return cooldown_elapsed(row.get("last_planned_at"), now, cooldown)


def parse_plan_verdict(raw: dict | None) -> tuple[bool, list[int], str]:
    """A planner's verdict as (goal_met, issue numbers, summary). Fails closed.

    Anything unreadable — no file, not a dict, garbage fields — comes back as
    `(False, [], ...)`, which `plan_outcome` routes to a stall rather than to `met`: a
    planner that produced nothing readable has declared nothing finished.
    """
    if not isinstance(raw, dict):
        return False, [], "no usable verdict from the planner"
    issues = []
    for n in raw.get("issues_created") or []:
        try:
            issues.append(int(n))
        except (TypeError, ValueError):
            continue
    return bool(raw.get("goal_met")), issues, str(raw.get("summary") or "").strip()


def plan_outcome(
    goal_met: bool, queued_now: bool, stalls: int, max_stalls: int
) -> tuple[str, int]:
    """Where a finished plan run leaves the repo's goal: (next state, next stall count).

    `queued_now` is what GitHub showed after the run, not what the verdict claimed, and it
    outranks the verdict in both directions. Issues queued means the loop continues whatever
    the planner said about the goal — a "goal met" beside freshly filed work is a
    contradiction that must fail toward building, never toward premature completion. No
    issues and no goal-met is a fruitless pass: the stall count rises, and at the cap the
    goal parks as `stalled` for a human instead of burning a VM every cooldown forever.
    """
    if queued_now:
        return repos.GOAL_ACTIVE, 0
    if goal_met:
        return repos.GOAL_MET, 0
    stalls += 1
    return (repos.GOAL_STALLED if stalls >= max_stalls else repos.GOAL_ACTIVE), stalls


# --------------------------------------------------------------------------- dispatch


async def maybe_plan(repo: str) -> str | None:
    """The poller's hook: plan `repo` if the goal loop says to, else do nothing.

    Reads the register cache, so a tick costs nothing when the answer is no — which is
    almost every tick.
    """
    if not should_plan(
        repos.row(repo), db.utcnow(), settings.plan_enabled, settings.plan_cooldown
    ):
        return None
    log.info("queue dry for %s; planning against its goal", repo)
    return await create(repo)


async def create(repo: str) -> str:
    """Register a plan run for `repo` and schedule it. Returns the run id.

    Also the manual path (`POST /api/runs` with `kind: "plan"`), which deliberately skips
    `should_plan`: a human asking for a plan now is the human overriding the cadence, the
    same way starting a build by hand overrides the poller. What it cannot override is a
    missing goal — there is nothing to plan toward.
    """
    repo = repo.strip()
    row = repos.row(repo)
    if row is None:
        raise ValueError(f"{repo} is not watched")
    goal = (row.get("goal") or "").strip()
    if not goal:
        raise ValueError(f"{repo} has no goal; set one before planning")

    run_id = uuid.uuid4().hex
    log_path = settings.log_dir / f"{run_id}.log"
    log_path.touch()
    await db.create_run(
        id=run_id,
        repo=repo,
        issue_number=0,
        issue_title=f"plan {repo}",
        status="queued",
        kind="plan",
        agent=agents.DEFAULT_AGENT,
        log_path=str(log_path),
        created_at=db.utcnow(),
    )
    # Stamped at dispatch, not completion: a plan run that crashes must still start the
    # cooldown, or a reliably-crashing planner re-dispatches every poll tick.
    await repos.record_plan_start(repo)
    runner.track(run_id, asyncio.create_task(_guarded(run_id, repo, goal)))
    return run_id


async def _record_stall(repo: str, run_log: runner.RunLog, reason: str) -> None:
    """Count a fruitless or crashed plan against the repo, parking it at the cap."""
    row = repos.row(repo) or {}
    stalls = int(row.get("plan_stalls") or 0)
    state, next_stalls = plan_outcome(False, False, stalls, settings.plan_max_stalls)
    await repos.record_plan_outcome(repo, state, next_stalls)
    if state == repos.GOAL_STALLED:
        run_log.write(
            f"[factory] goal stalled after {next_stalls} fruitless plans ({reason}); "
            "a human re-activates it from the Projects page"
        )
    else:
        run_log.write(
            f"[factory] fruitless plan {next_stalls}/{settings.plan_max_stalls} ({reason})"
        )


async def _guarded(run_id: str, repo: str, goal: str) -> None:
    run_log = runner.RunLog(Path(settings.log_dir / f"{run_id}.log"))
    try:
        # The build semaphore, not provisioning's: a plan run is an agent spending tokens,
        # and how many agents run at once is exactly what that budget is.
        async with runner.semaphore():
            await _execute(run_id, repo, goal, run_log)
        # Terminal only now, the ordering `_guarded_review` documents: everything the run
        # decided is recorded before the repo has no non-terminal run again.
        await db.update_run(run_id, status="succeeded", finished_at=db.utcnow())
    except asyncio.CancelledError:
        # A human stopped it. Not a stall — cancellation is a decision, not a failure.
        run_log.write("[factory] plan run cancelled")
        await db.update_run(run_id, status="cancelled", finished_at=db.utcnow())
        raise
    except Exception as exc:  # noqa: BLE001 - the UI is where failures get reported
        run_log.write(f"[factory] plan run failed: {exc!r}")
        if isinstance(exc, asyncio.TimeoutError):
            reason = f"timed out after {settings.run_timeout}s"
        else:
            reason = f"crashed: {str(exc)[:200] or type(exc).__name__}"
        await db.update_run(run_id, status="failed", error=reason, finished_at=db.utcnow())
        await _record_stall(repo, run_log, reason)
    finally:
        await runner._salvage_usage(run_id, run_log)
        run_log.close()


async def _execute(run_id: str, repo: str, goal: str, run_log: runner.RunLog) -> None:
    boxd = runner.client()
    machine = None
    reaped = False
    try:
        base = await github.default_branch(repo)
        notes = await runner.project_notes(repo, base)

        await db.update_run(run_id, status="forking", started_at=db.utcnow())
        vm_name = f"{runner.PLAN_PREFIX}{run_id[:8]}"
        run_log.write(f"[factory] planning {repo} against its goal")
        await runner.headroom(boxd, run_log)
        source = await runner.source_for(boxd, repo, run_log)
        await db.update_run(run_id, golden=source)
        run_log.write(f"[factory] provisioning {vm_name} from {source}")
        machine = await runner._provision(boxd, source, vm_name)
        await db.update_run(run_id, vm_name=machine.name, vm_id=machine.id)
        await boxd.machines.wait_until_ready(machine.id, timeout=180)
        run_log.write(f"[factory] {machine.name} ready ({machine.id})")

        await db.update_run(run_id, status="running")
        prompt = PLAN_PROMPT_TEMPLATE.format(
            repo=repo,
            base=base,
            goal=goal,
            max_issues=settings.plan_max_issues,
            project_notes=notes,
        )
        env = runner.dispatch_env(
            repo=repo,
            branch=base,
            base=base,
            prompt=prompt,
            run_id=run_id,
            number=0,
            vm_name=machine.name,
            kind="plan",
        )
        exit_code, usage, manifest = await asyncio.wait_for(
            runner._stream(boxd, machine.id, env, run_log, run_id, script=PLAN_SCRIPT),
            timeout=settings.run_timeout,
        )
        run_log.write(f"[factory] planner exited {exit_code}")
        verdict = await runner._read_verdict(
            boxd, machine.id, run_log, path=PLAN_VERDICT_PATH
        )
        await runner._salvage_transcript(boxd, machine.id, run_id, run_log, manifest)

        goal_met, claimed, summary = parse_plan_verdict(verdict)
        await db.update_run(
            run_id,
            exit_code=exit_code,
            tokens_in=usage.get("tokens_in"),
            tokens_out=usage.get("tokens_out"),
            cost_usd=usage.get("cost_usd"),
            verdict=json.dumps(verdict) if verdict else None,
        )

        await asyncio.sleep(3)
        await runner.reap(boxd, machine, run_log)
        reaped = True

        # What is true is what GitHub shows, not what the verdict says. A claimed issue that
        # never appears with the label is a plan that did not happen; a queued issue outranks
        # a "goal met". The check costs one list call the poller was about to make anyway.
        queued_now = bool(await github.list_issues_with_label(repo, github.LABEL_QUEUED))
        if claimed:
            run_log.write(
                f"[factory] planner filed {', '.join(f'#{n}' for n in claimed)}"
                + ("" if queued_now else " — but none of them are queued")
            )
        if summary:
            run_log.write(f"[factory] planner: {summary}")

        if queued_now or goal_met:
            row = repos.row(repo) or {}
            state, stalls = plan_outcome(
                goal_met, queued_now, int(row.get("plan_stalls") or 0),
                settings.plan_max_stalls,
            )
            await repos.record_plan_outcome(repo, state, stalls)
            if state == repos.GOAL_MET:
                run_log.write("[factory] goal met; this repo is done until its goal changes")
            else:
                run_log.write("[factory] issues queued; the poller dispatches the lowest next tick")
        else:
            reason = summary or "no issues filed and the goal not declared met"
            await db.update_run(run_id, error=f"{PLAN_FRUITLESS}{reason[:300]}")
            await _record_stall(repo, run_log, reason[:120])
    finally:
        if not reaped:
            await runner.reap(boxd, machine, run_log, keep=settings.keep_failed)
        await boxd.close()
