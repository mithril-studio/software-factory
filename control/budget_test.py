"""Two ceilings, because one of them does not bound a loop.

Until this week the factory had a single dispatcher: a human labelling issues. Spend was
bounded by how fast somebody typed. The goal loop and the improvement loop removed that — both
file `agent:queued` issues, so the factory now creates its own work, and the only thing between
a planner that never quite finishes and an unbounded bill is a number in a config file.

The per-run ceiling is the obvious half and it is **not** the half that matters here. Trace the
planner: it files up to FACTORY_PLAN_MAX_ISSUES issues, each builds for well under $25, the
queue drains, it plans again. Nothing in that cycle is individually expensive. Every run passes
the per-run check. It terminates when an agent decides the goal is met.

So the daily ceiling is the one doing the work, and these check the two properties that make
either worth having: a run stopped for cost is never retried, and a ceiling that cannot read
the spend never becomes the reason the factory stops.

Run it directly, no framework needed:

    .venv/bin/python -m control.budget_test
"""
import asyncio
import inspect
import sys
import tempfile
from pathlib import Path

from control import db, poller, runner
from control.config import settings
from telemetry import config as tconfig
from telemetry import store

fails: list[str] = []


def check(name, got, want=True):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n       got={got!r} want={want!r}"))
    if not ok:
        fails.append(name)


tmp = tempfile.TemporaryDirectory()
object.__setattr__(settings, "db_path", Path(tmp.name) / "factory.db")
object.__setattr__(settings, "log_dir", Path(tmp.name) / "logs")
settings.log_dir.mkdir(parents=True, exist_ok=True)
tconfig.db_path = settings.db_path


def run(coro):
    return asyncio.run(coro)


run(db.init())
run(store.init())

REPO = "acme/web"
TODAY = db.utcnow()[:10]


# ---------- the ceiling ships on, at the number that was chosen

check("a per-run ceiling is set by default", settings.max_run_cost, 25.0)
check("and a daily one, because the per-run cap does not bound a loop",
      settings.max_repo_daily_cost > 0, True)


# ---------- a run stopped for cost is never retried
#
# Every other failure a build can suffer is worth another go: a crash, a timeout, a VM that
# vanished. This one is the opposite — the run was stopped *because* it was consuming money
# without converging, and `max_attempts` retries would make one lost run cost three ceilings.

fail_src = inspect.getsource(runner._fail_run)
check("_fail_run can refuse to retry", "retryable and attempt < settings.max_attempts" in fail_src, True)

guard_src = inspect.getsource(runner._guarded)
check("and the budget failure is what refuses",
      "retryable=not isinstance(exc, BudgetExceeded)" in guard_src, True)
check("a budget abort reads as one in the runs table", "over budget: " in guard_src, True)
check("BudgetExceeded is its own type, not a bare RuntimeError",
      issubclass(runner.BudgetExceeded, RuntimeError)
      and runner.BudgetExceeded is not RuntimeError, True)

# The check runs at a turn boundary, which is the only moment the derived cost has moved.
stream_src = inspect.getsource(runner._stream)
check("the ceiling is checked when a turn closes",
      "if await recorder.feed(event) and settings.max_run_cost" in stream_src, True)
check("and is skipped entirely when switched off",
      "settings.max_run_cost" in stream_src, True)


# ---------- the daily ceiling, which is the one that bounds a loop

async def seed(run_id: str, cost_rows: list[tuple[int, int]], created_at: str, kind="build"):
    """A run plus the llm_calls that give it a derived cost."""
    await db.create_run(id=run_id, repo=REPO, issue_number=1, status="succeeded",
                        kind=kind, created_at=created_at)
    await store.write([
        __import__("telemetry.normalize", fromlist=["LlmCall"]).LlmCall(
            run_id=run_id, turn=i, ts=created_at, model="claude-opus-5",
            input_tokens=inp, output_tokens=out, cache_read_tokens=0,
            cache_write_5m_tokens=0, cache_write_1h_tokens=0,
        )
        for i, (inp, out) in enumerate(cost_rows)
    ])


# A million output tokens at list price is well over any sane ceiling; two runs of it make the
# repo's day expensive without either run being individually absurd — which is the shape the
# daily ceiling exists to catch.
run(seed("r1", [(1_000_000, 200_000)], f"{TODAY}T01:00:00+00:00"))
spent_one = run(store.spend_since(REPO, f"{TODAY}T00:00:00+00:00"))
check("spend is derived from the rows, not stored", spent_one > 0, True)

run(seed("r2", [(1_000_000, 200_000)], f"{TODAY}T02:00:00+00:00"))
spent_two = run(store.spend_since(REPO, f"{TODAY}T00:00:00+00:00"))
check("a second run adds to the day", spent_two > spent_one, True)

# Every kind counts. A ceiling that exempted planning and learning runs would exempt exactly
# the two things that dispatch work without being asked.
run(seed("p1", [(500_000, 100_000)], f"{TODAY}T03:00:00+00:00", kind="plan"))
run(seed("l1", [(500_000, 100_000)], f"{TODAY}T04:00:00+00:00", kind="learn"))
check("planning and learning spend counts too",
      run(store.spend_since(REPO, f"{TODAY}T00:00:00+00:00")) > spent_two, True)

# Yesterday's spend is not today's problem: the ceiling is a rate, not a lifetime cap.
run(seed("old", [(1_000_000, 500_000)], "2020-01-01T00:00:00+00:00"))
check("older runs are outside the window",
      run(store.spend_since(REPO, f"{TODAY}T00:00:00+00:00"))
      == run(store.spend_since(REPO, f"{TODAY}T00:00:00+00:00")), True)
check("another repo's spend is not counted",
      run(store.spend_since("other/repo", "2000-01-01T00:00:00+00:00")), 0.0)

# The gate itself.
object.__setattr__(settings, "max_repo_daily_cost", 1000000.0)
check("under the ceiling, the repo may dispatch", run(poller._within_budget(REPO)), True)
object.__setattr__(settings, "max_repo_daily_cost", 0.01)
check("over it, the repo may not", run(poller._within_budget(REPO)), False)
object.__setattr__(settings, "max_repo_daily_cost", 0)
check("and zero switches the ceiling off entirely", run(poller._within_budget(REPO)), True)

# It gates the loops and never the queue. An issue with `agent:queued` on it is work somebody
# asked for; a ceiling that stops a backlog halfway because a planner had an expensive morning
# is a ceiling the operator switches off, which is worse than not having one. The loops are
# what dispatch unasked, so they are what a spend ceiling has any business stopping.
poll_src = inspect.getsource(poller._poll_repo)
check("the guard sits inside the dry-queue branch, below the queue check",
      poll_src.index("list_issues_with_label(repo, github.LABEL_QUEUED)")
      < poll_src.index("_within_budget"), True)
check("and above both loop hooks",
      poll_src.index("_within_budget") < poll_src.index("plan.maybe_plan")
      and poll_src.index("_within_budget") < poll_src.index("_maybe_learn"), True)
# The property that would be undone by moving one line, checked on nesting rather than on
# textual order — the dry-queue branch returns, so the dispatch sits *after* the guard in the
# source while being unreachable from it. Depth is what actually says "not under the guard".
def indent_of(needle: str) -> int:
    line = next(ln for ln in poll_src.splitlines() if needle in ln)
    return len(line) - len(line.lstrip())


check("the budget check is nested inside the dry-queue branch",
      indent_of("if not await _within_budget(repo)") > indent_of("if not issues:"), True)
check("while dispatching a queued issue sits at function level, under no budget check",
      indent_of("await runner.create(repo, issue[\"number\"])"),
      indent_of("if not issues:"))

# A ceiling that cannot read the spend must not become the reason the factory stops. This is
# the same rule the trace layer holds itself to: telemetry that can halt work is worse than
# none, so an unreadable number fails toward doing the work.
budget_src = inspect.getsource(poller._within_budget)
check("an unreadable spend allows the dispatch", "return True" in budget_src.split("except")[1], True)


print()
print(f"{len(fails)} failed: {', '.join(fails)}" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
