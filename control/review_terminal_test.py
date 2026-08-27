"""When a review run becomes terminal, and why it must not be the moment the verdict is read.

`poller._poll_repo` holds a repo to one run at a time, which is what makes a numbered backlog
sequential: #2 does not start until #1 has reached a terminal state, retries included. The
guard is `db.has_active_run`, so the guarantee is only as good as when a run stops counting.

`_execute_review` used to write `status="succeeded"` as soon as it had the verdict, and *then*
merge the pull request — up to `FACTORY_MERGE_CHECK_TIMEOUT` — or create a fix run. For the
whole of that window the repo had no non-terminal run. On 2026-08-21 that dispatched
foundation-e-learning #71's fix run and #72's first build in the same second, both branched
from a main that contained neither.

The build path never had the bug and says why at `_fail_or_retry`: "The retry is created
*before* this run is marked terminal, so the repo never looks idle to the poller in the gap."
This pins the same rule onto the review path, from the only angle that matters — what
`has_active_run` answers while the work is still being scheduled.

Run it directly, no framework needed:

    .venv/bin/python -m control.review_terminal_test
"""
import asyncio
import inspect
import sys
import tempfile
from pathlib import Path

from control import db, runner
from control.config import settings

fails = []


def check(name, got, want=True):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n       got={got!r} want={want!r}"))
    if not ok:
        fails.append(name)


tmp = tempfile.TemporaryDirectory()
object.__setattr__(settings, "db_path", Path(tmp.name) / "factory.db")
object.__setattr__(settings, "log_dir", Path(tmp.name) / "logs")
settings.log_dir.mkdir(parents=True, exist_ok=True)
asyncio.run(db.init())

REPO = "acme/api"


def run(coro):
    return asyncio.run(coro)


# ---------- the source-level rule
#
# Read off the source rather than by driving a review to completion: `_execute_review` needs a
# VM, a reviewer, GitHub and a merge to get as far as the bug, and a test that stubbed all four
# would be asserting against its own stubs. What went wrong was one keyword argument in the
# wrong call, and that is exactly what this can see.

review_src = inspect.getsource(runner._execute_review)
guard_src = inspect.getsource(runner._guarded_review)

check("the verdict write does not mark the run terminal",
      'status="succeeded"' in review_src, False)
check("nor stamp it finished",
      "finished_at=db.utcnow()" in review_src, False)
check("the verdict itself is still recorded there",
      "verdict=json.dumps(verdict)" in review_src, True)
check("the run is marked terminal by the guard instead",
      'status="succeeded"' in guard_src, True)

# The ordering that makes it correct: terminal only after `_execute_review` has returned, which
# it does once the merge was attempted or the fix run created.
call = guard_src.index("await _execute_review(")
mark = guard_src.index('status="succeeded"')
check("and only after the work it decided has been scheduled", call < mark, True)

# On the failure paths the guard still owns the outcome, so a crashed or cancelled review can
# never leave a repo blocked behind a run that is neither running nor finished.
check("a crashed review is still marked terminal", 'status="failed"' in guard_src, True)
check("a cancelled one too", 'status="cancelled"' in guard_src, True)


# ---------- what the poller actually asks
#
# The rule above only matters because of this: a review that is not terminal keeps its repo
# claimed, and one that is releases it.

run(db.create_run(id="b1", repo=REPO, issue_number=71, status="succeeded",
                  kind="build", created_at=db.utcnow()))
run(db.create_run(id="r1", repo=REPO, issue_number=71, status="running",
                  kind="review", created_at=db.utcnow()))
check("a review still deciding keeps the repo claimed", run(db.has_active_run(REPO)), True)

run(db.update_run("r1", status="succeeded", finished_at=db.utcnow()))
check("and releases it once it is terminal", run(db.has_active_run(REPO)), False)

# The window the bug lived in, stated as the poller sees it: with the review already terminal
# and the fix run not yet created, the repo is idle and #72 is claimable. Nothing can close
# that gap except not opening it.
run(db.create_run(id="b2", repo=REPO, issue_number=71, status="queued",
                  kind="build", created_at=db.utcnow()))
check("a fix run created afterwards re-claims it", run(db.has_active_run(REPO)), True)

# Provisioning stays excluded, so warming a golden never blocks the repo it was connected for.
run(db.update_run("b2", status="succeeded", finished_at=db.utcnow()))
run(db.create_run(id="p1", repo=REPO, issue_number=0, status="running",
                  kind="provision", created_at=db.utcnow()))
check("a warm-up in flight still does not claim the repo", run(db.has_active_run(REPO)), False)

# A learning run is excluded for the same reason and one of its own. It claims no issue, so
# counting it would stop the repo for the duration of something that was never in the way —
# and it reads a window of *finished* work, so the builds it would be blocking are the ones
# that extend the window it is summarising. Blocking them buys nothing and costs a queue.
run(db.create_run(id="l1", repo=REPO, issue_number=0, status="running",
                  kind="learn", created_at=db.utcnow()))
check("a learning run does not claim the repo either", run(db.has_active_run(REPO)), False)

# Both exclusions come from one list rather than two `!=` clauses that drifted apart.
check("the excluded kinds are named in one place", sorted(db.UNCLAIMED_KINDS),
      ["learn", "provision"])

# A build alongside them still claims it. Without this the previous two checks would also
# pass if the guard had simply stopped working.
run(db.create_run(id="b3", repo=REPO, issue_number=73, status="running",
                  kind="build", created_at=db.utcnow()))
check("a build alongside them still claims the repo", run(db.has_active_run(REPO)), True)


tmp.cleanup()
print()
print(f"{len(fails)} failed" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
