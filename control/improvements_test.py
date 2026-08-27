"""A loop that cannot say why it changed something cannot be allowed to delete anything.

The improvement loop edits the files that decide how agents behave. Those edits arrive as
pull requests, so git records what changed — but git cannot record which runs were the
evidence, which number the change was meant to move, or what that number did afterwards.
An unattributable rule is one nobody can safely remove, and a system that only accumulates
rules gets worse at the thing the rules were added to improve.

So the ledger enforces the parts a model under pressure to produce three proposals would
otherwise leave blank, and it refuses to treat `merged` as the end of the story. Both are
checked here, along with the concurrency guard that stops two graders disagreeing quietly.

Run it directly, no framework needed:

    .venv/bin/python -m control.improvements_test
"""
import asyncio
import sys
import tempfile
from pathlib import Path

from control import db
from control.config import settings

fails: list[str] = []


def check(name, got, want=True):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n       got={got!r} want={want!r}"))
    if not ok:
        fails.append(name)


def raises(name, fn, needle=None):
    try:
        fn()
    except ValueError as exc:
        if needle and needle not in str(exc):
            check(f"{name} (message)", str(exc), f"...{needle}...")
            return
        check(name, True)
        return
    check(name, "no error raised", "ValueError")


tmp = tempfile.TemporaryDirectory()
object.__setattr__(settings, "db_path", Path(tmp.name) / "factory.db")
object.__setattr__(settings, "log_dir", Path(tmp.name) / "logs")
settings.log_dir.mkdir(parents=True, exist_ok=True)


def run(coro):
    return asyncio.run(coro)


run(db.init())


def proposal(**over):
    base = dict(
        id="imp_1", repo="acme/web", run_id="learn-1", artifact="skill", action="add",
        target=".claude/skills/verify-before-pr/SKILL.md",
        rationale="Six runs opened a PR without running the repo's verify script.",
        evidence='{"run_ids": ["r1", "r2", "r3"], "signature": "ci red: typecheck"}',
        metric="review_rejection_rate", baseline=0.42,
    )
    return {**base, **over}


# ---------- AC1: a proposal must justify itself

# Each of these is a field the ledger exists to hold. A row missing one is a row that can
# never be graded, and therefore never deleted — it would enter the store already immune.
for field in ("rationale", "evidence", "metric", "run_id"):
    raises(f"a proposal with no {field} is refused",
           lambda f=field: run(db.create_improvement(**proposal(**{f: ""}))), field)

raises("an unknown artifact is refused",
       lambda: run(db.create_improvement(**proposal(artifact="prompt"))), "unknown artifact")
raises("an unknown action is refused",
       lambda: run(db.create_improvement(**proposal(action="tweak"))), "unknown action")
raises("an unknown column is refused",
       lambda: run(db.create_improvement(**proposal(reason="x"))), "unknown improvements column")

# The door this closes: a proposal that could name its own status could arrive already
# `merged`, having skipped the review and CI that are the only things checking it.
raises("a proposal cannot be born merged",
       lambda: run(db.create_improvement(**proposal(status="merged"))), "use transition_improvement")


# ---------- AC2: the lifecycle, and why merged is not the end of it

run(db.create_improvement(**proposal()))
row = run(db.get_improvement("imp_1"))
check("a valid proposal lands", row["status"], "proposed")
check("with its evidence intact", '"r1"' in row["evidence"], True)
check("and its baseline", row["baseline"], 0.42)

run(db.transition_improvement("imp_1", "building", issue_url="https://gh/issues/12"))
check("picked up by the factory", run(db.get_improvement("imp_1"))["status"], "building")
check("the issue it became is recorded",
      run(db.get_improvement("imp_1"))["issue_url"], "https://gh/issues/12")

run(db.transition_improvement("imp_1", "merged", pr_url="https://gh/pull/13"))
merged = run(db.get_improvement("imp_1"))
check("merged is reached", merged["status"], "merged")
# The assertion the whole design rests on: a merged change is live, not finished. If merged
# were terminal there would be no state in which "was it worth it?" is still an open question,
# and every rule the loop ever added would stay forever by default.
check("merged is not terminal", "merged" in db.IMPROVEMENT_TRANSITIONS, True)
check("it is still ungraded", (merged["observed"], merged["graded_at"]), (None, None))

raises("a merged change cannot skip back to building",
       lambda: run(db.transition_improvement("imp_1", "building")))

graded = run(db.transition_improvement("imp_1", "kept", observed=0.19))
check("grading records what actually happened", graded["observed"], 0.19)
check("and stamps when", bool(graded["graded_at"]), True)
check("kept is terminal", db.IMPROVEMENT_TRANSITIONS.get("kept", ()), ())

# Grading *is* the transition. Were `observed` writable without moving the status, a row could
# hold a measurement while still claiming to be ungraded, and the grader would keep re-reading it.
raises("a graded row cannot be graded twice",
       lambda: run(db.transition_improvement("imp_1", "reverted", observed=0.5)))


# ---------- AC3: a change that did not earn its place

run(db.create_improvement(**proposal(id="imp_2", action="edit", metric="ship_nothing_rate",
                                     baseline=0.30)))
run(db.transition_improvement("imp_2", "building"))
run(db.transition_improvement("imp_2", "merged"))
run(db.transition_improvement("imp_2", "reverted", observed=0.31))
check("a change that moved nothing can be reverted",
      run(db.get_improvement("imp_2"))["status"], "reverted")

# A revert is an ordinary change, not a status flag: it needs its own issue, its own review,
# and its own row saying what it undid. That is why `revert` is in the action set.
check("revert is an action a later proposal can take", "revert" in db.IMPROVEMENT_ACTIONS, True)


# ---------- AC4: history is what stops the loop oscillating

run(db.create_improvement(**proposal(id="imp_3", repo="acme/api")))
run(db.transition_improvement("imp_3", "rejected"))

ledger = run(db.list_improvements())
check("every proposal is in the ledger, including the failures", len(ledger), 3)
check("newest first", ledger[0]["id"], "imp_3")
# Without the rejected and reverted rows the next learning run has no way to know it already
# tried something, so it proposes it again, it is rejected again, and the loop never converges.
check("rejected work is visible to the next run",
      {r["status"] for r in ledger}, {"kept", "reverted", "rejected"})

check("the ledger scopes by repo",
      [r["id"] for r in run(db.list_improvements(repo="acme/api"))], ["imp_3"])
check("and by status",
      [r["id"] for r in run(db.list_improvements(status="reverted"))], ["imp_2"])

# Re-recording the same id must not reset a status somebody already moved on from — an agent
# retrying a proposal it already filed is an ordinary event, not a reason to un-grade it.
run(db.create_improvement(**proposal(id="imp_1")))
check("re-filing a proposal leaves the original alone",
      run(db.get_improvement("imp_1"))["status"], "kept")


# ---------- AC5: two writers, one winner

async def race():
    await db.create_improvement(**proposal(id="imp_race"))
    await db.transition_improvement("imp_race", "building")
    await db.transition_improvement("imp_race", "merged")
    results = await asyncio.gather(
        db.transition_improvement("imp_race", "kept", observed=0.1),
        db.transition_improvement("imp_race", "reverted", observed=0.9),
        return_exceptions=True,
    )
    return [type(r) is not dict for r in results]

losers = run(race())
check("exactly one of two simultaneous gradings wins", sum(losers), 1)
check("and the row holds one verdict, not a blend",
      run(db.get_improvement("imp_race"))["status"] in ("kept", "reverted"), True)


tmp.cleanup()
print()
print(f"{len(fails)} failed: {', '.join(fails)}" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
