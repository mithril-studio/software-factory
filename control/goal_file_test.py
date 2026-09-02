"""The goal-file sync's shape: where it sits in the poll, and how it fails.

Source and AST checks in the `budget_test.py` style, because every property here is one a
refactor could undo by moving a line while every test still passed:

1. **The sync sits between the queue check and the budget gate.** Above the gate because one
   contents-API GET spends no model tokens — a repo past its daily ceiling must still wake
   up when its goal file changes. Below the queue check because a queued issue means there
   is nothing for a goal transition to decide this tick.
2. **The throttle is stamped before the fetch.** Same reasoning as `record_plan_start`: a
   GitHub that errors gets retried once per interval, not once per poll tick.
3. **An error makes no transition.** "Unknown" must never read as "the goal was deleted", or
   a GitHub incident clears every goal on the board. The 404 path is different — that is a
   definite absence, answered inside `file_sha` itself.

Run it directly, no framework needed:

    .venv/bin/python -m control.goal_file_test
"""
import ast
import inspect
import sys
import textwrap

from control import github, plan, poller

fails = []


def check(name, got, want=True):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n       got={got!r} want={want!r}"))
    if not ok:
        fails.append(name)


# ---------- where the sync sits in _poll_repo

poll_src = inspect.getsource(poller._poll_repo)
check("the sync sits below the queue check",
      poll_src.index("list_issues_with_label(repo, github.LABEL_QUEUED)")
      < poll_src.index("plan.sync_goal_file"), True)
check("and above the budget gate — a spend-free check an over-budget repo still gets",
      poll_src.index("plan.sync_goal_file") < poll_src.index("_within_budget"), True)


def indent_of(needle: str) -> int:
    line = next(ln for ln in poll_src.splitlines() if needle in ln)
    return len(line) - len(line.lstrip())


check("the sync is nested inside the dry-queue branch",
      indent_of("plan.sync_goal_file") > indent_of("if not issues:"), True)


# ---------- how the sync itself is shaped

sync_src = inspect.getsource(plan.sync_goal_file)
check("the sync is a no-op while the goal loop is switched off",
      "settings.plan_enabled" in sync_src)
check("the throttle is stamped before the fetch, so a failing API is hit once per interval",
      sync_src.index("_goal_synced[repo]") < sync_src.index("github.file_sha"), True)

sync_fn = ast.parse(textwrap.dedent(sync_src)).body[0]
handlers = [h for t in ast.walk(sync_fn) if isinstance(t, ast.Try) for h in t.handlers]
check("the fetch is guarded at all", len(handlers) >= 1)
check("and its error path makes no transition — unknown is not deleted",
      any("apply_goal_file" in ast.unparse(h) for h in handlers), False)
check("but ends the sync instead of falling through to one",
      all(isinstance(h.body[-1], ast.Return) for h in handlers))


# ---------- file_sha: three answers, kept distinct

sha_src = inspect.getsource(github.file_sha)
check("a 404 is a definite absence, answered with None",
      sha_src.index("404") < sha_src.index("raise_for_status"), True)
check("anything else raises rather than reading as absence",
      "raise_for_status" in sha_src)
check("an empty file is no goal", "size" in sha_src)


print()
print(f"{len(fails)} failed" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
