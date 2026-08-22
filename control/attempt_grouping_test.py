"""One attempt at an issue is one row in the log, and CI is one of its phases.

The dispatch log listed VM dispatches in reverse time order and called each of them a run. So
foundation-e-learning#77 read, top to bottom: review **changes requested**, build **succeeded**,
review **changes requested**, build **succeeded** — which looks like a status moving backwards
and a build being retried after it worked. Nothing was moving. They were four separate rows for
two passes at one issue, and the flat list had no way to say so.

Underneath that was a second gap: CI is the one thing that judges a run and had no record of its
own. Its verdict was a string on the *review's* `error` column and its log went nowhere but the
next agent's prompt, so a build could sit in the log reading `succeeded` above the review that
sent it back, and "red on what?" was a question you had to leave the factory to answer.

Both halves are checked here, and the second half of each is a contract with a file in another
language — `web/src/lib/api.ts` decides what an attempt's status is, `control/db.py` decides what
an attempt *is*, and nothing but this file can see both.

Run it directly, no framework needed:

    .venv/bin/python -m control.attempt_grouping_test
"""
import asyncio
import inspect
import pathlib
import sys
import tempfile

from control import db, runner
from control.config import settings

fails: list[str] = []


def check(name, got, want=True):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n       got={got!r} want={want!r}"))
    if not ok:
        fails.append(name)


ROOT = pathlib.Path(__file__).resolve().parent.parent
API_TS = (ROOT / "web" / "src" / "lib" / "api.ts").read_text()
RUNS_TSX = (ROOT / "web" / "src" / "pages" / "Runs.tsx").read_text()
APP_PY = inspect.getsource(__import__("control.app", fromlist=["app"]))


# --------------------------------------------------------------- grouping, against a real DB

async def _seed_and_group():
    """Two cycles of one issue, plus a provisioning run, in the order the factory writes them."""
    rows = [
        # cycle 1: a build that crashed, the retry that worked, the CI that judged it, the
        # review that approved it anyway.
        dict(id="b1", kind="build", cycle=1, attempt=1, status="failed", created_at="2026-08-21T14:37:00+00:00"),
        dict(id="b2", kind="build", cycle=1, attempt=2, status="succeeded", created_at="2026-08-21T14:40:00+00:00"),
        dict(id="c1", kind="ci", cycle=1, attempt=1, status="failed", error="checks failed: gates=failure", created_at="2026-08-21T14:58:00+00:00"),
        dict(id="r1", kind="review", cycle=1, attempt=1, status="succeeded", error="ci red: checks failed: gates=failure", created_at="2026-08-21T14:55:00+00:00"),
        # cycle 2: the fix, and the review that stopped it.
        dict(id="b3", kind="build", cycle=2, attempt=1, status="succeeded", created_at="2026-08-21T15:03:00+00:00"),
        dict(id="r2", kind="review", cycle=2, attempt=1, status="succeeded", error="changes requested: no isolation test", created_at="2026-08-21T15:34:00+00:00"),
    ]
    for row in rows:
        await db.create_run(
            repo="mithril-studio/foundation-e-learning", issue_number=77,
            issue_title="Build the learner dashboard view model", **row,
        )
    # Two provisionings of the same repo's golden. Neither is an attempt at an issue, and they
    # are not each other's phases.
    for i, when in enumerate(["2026-08-21T13:00:00+00:00", "2026-08-21T13:30:00+00:00"]):
        await db.create_run(
            id=f"p{i}", repo="mithril-studio/specter-ai", issue_number=0, kind="provision",
            cycle=1, attempt=1, status="succeeded", created_at=when,
        )
    return await db.list_attempts(), await db.list_attempts(limit=1)


with tempfile.TemporaryDirectory() as tmp:
    object.__setattr__(settings, "db_path", pathlib.Path(tmp) / "factory.db")
    asyncio.run(db.init())
    attempts, first_only = asyncio.run(_seed_and_group())

ids = [[p["id"] for p in a["phases"]] for a in attempts]

check("one issue's cycle is one attempt, not four rows", ids[0], ["b3", "r2"])
check("its earlier cycle is a separate attempt", ids[1], ["b1", "b2", "r1", "c1"])
# The regression: a crash retry and a fix cycle used to be the same number, so `attempt`
# alone could not tell "try again" from "go round again" and both cycles grouped as one.
check("crash retries stay inside their cycle", "b1" in ids[1] and "b2" in ids[1])
check("phases read in causal order, oldest first",
      ids[1] == sorted(ids[1], key=lambda i: {"b1": 0, "b2": 1, "r1": 2, "c1": 3}[i]))
check("attempts read newest first", ids[0][0] == "b3")
# Grouping by repo would have made every golden this repo ever built into one attempt.
check("provisioning runs are one attempt each", ids[2:], [["p1"], ["p0"]])
# The reason this is a query and not a groupBy in the browser: paginate on rows and the
# attempt straddling the boundary loses half its phases, leaving a build with no review
# under it — a run that looks like it succeeded and stopped.
check("the limit counts attempts, not rows", [p["id"] for p in first_only[0]["phases"]], ["b3", "r2"])


# --------------------------------------------------------------- CI is recorded as a phase

src = inspect.getsource(runner)

check("there is something that writes a CI phase", "async def _record_ci(" in src)
check("it is written as its own kind of run", 'kind="ci",' in src)
check("red checks make it a failed run", '"succeeded" if green else "failed"' in src)
check("and it keeps the failing log where the interface can read it", "log_path=str(log_path)" in inspect.getsource(runner._record_ci))
# The point of putting the call in `_merge` rather than at either caller: a phase written in
# one branch of one caller is a phase missing from the other.
check("the merge path is what records it", "_record_ci(" in inspect.getsource(runner._merge))
check("bookkeeping never fails a run", "could not record the CI phase" in src)
# Two fetches of one log is also two *answers*: the second reads whatever the head is by
# then, not the commit the checks actually judged.
check("the fix prompt reuses that log instead of fetching it again",
      ("merge_attempt.ci_log" in src, "github.failing_check_logs(repo, merge_attempt.head_sha)" in src),
      (True, False))
check("a CI phase carries no VM, agent or cost columns",
      any(k in inspect.getsource(runner._record_ci) for k in ("vm_name=", "agent=", "cost_usd=")),
      False)


# --------------------------------------------------------------- the contract across languages

check("the server groups runs into attempts", "async def list_attempts(" in inspect.getsource(db))
check("a run with no issue is its own group", "ELSE id END" in db.ATTEMPT_KEY)
check("the API serves them", '@app.get("/api/attempts")' in APP_PY)
check("the runs page reads that endpoint", '"/api/attempts"' in RUNS_TSX)
check("an attempt's status is a thing the client computes", "export function attemptOutcome" in API_TS)
# The rule, and the whole fix: a green build under a red CI is not a green attempt, because
# the phase that came last is the one still true.
check("it takes the last phase's outcome", "a.phases[a.phases.length - 1]" in API_TS)
# So the cycle-1 attempt above — build ok, CI red, reviewer approved anyway — is "ci red",
# which is the row that used to read "succeeded".
check("a failed CI phase reads as red CI, not as a broken dispatch",
      'r.kind === "ci"' in API_TS and '"ci red"' in API_TS)
check("anything unfinished outranks it", 'return "running"' in API_TS)
check("the phase strip and the badge agree on what is bad", "stateVariant" in RUNS_TSX)


print()
if fails:
    print(f"{len(fails)} failed: " + ", ".join(fails))
    sys.exit(1)
print("all passed")
