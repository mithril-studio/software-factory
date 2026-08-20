"""The memory candidate queue: admission control between an agent noticing something and the
factory treating it as durable truth.

A candidate is evidence-backed and scoped to the run and repo that produced it. It starts
`pending` and may move exactly once, to `accepted` or `rejected` — both terminal, so a second
transition of any kind (including a repeat of the same one) must fail rather than silently
succeed. Nothing here promotes a candidate into a repo's own `.mem/`; that is a later step.

This one does touch SQLite, against a throwaway file, the same way `repos_test.py` does: the
failures this guards against (idempotent insert, transition edges) live in the database, not
in a stub.

Run it directly, no framework needed:

    .venv/bin/python -m control.memory_candidates_test
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

from control import db
from control.config import settings

fails = []


def check(name, got, want=True):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n       got={got!r} want={want!r}"))
    if not ok:
        fails.append(name)


tmp = tempfile.TemporaryDirectory()
object.__setattr__(settings, "db_path", Path(tmp.name) / "factory.db")
asyncio.run(db.init())


def run(coro):
    return asyncio.run(coro)


REPO = "mithril-studio/software-factory"


def make(candidate_id, **overrides):
    fields = dict(
        id=candidate_id,
        run_id="run-1",
        repo=REPO,
        domain="repository",
        type="convention",
        title="Fast verification is ruff --select F,E9",
        body="CI's only gate for control/ is ruff --select F,E9 plus __main__ test modules.",
        evidence=json.dumps({"files": ["control/db.py"], "issues": ["mithril-studio/software-factory#52"]}),
        confidence="high",
    )
    fields.update(overrides)
    return fields


# ---------- candidate round trip

run(db.create_candidate(**make("cand-1")))
got = run(db.get_candidate("cand-1"))
check("round trip: the candidate exists", got is not None)
check("round trip: its run is intact", got["run_id"], "run-1")
check("round trip: its repository is intact", got["repo"], REPO)
check("round trip: its evidence is intact",
      json.loads(got["evidence"]), {"files": ["control/db.py"], "issues": ["mithril-studio/software-factory#52"]})
check("round trip: its confidence is intact", got["confidence"], "high")
check("round trip: it starts pending", got["status"], "pending")
check("round trip: it shows up in a repo-scoped listing",
      [c["id"] for c in run(db.list_candidates(repo=REPO))], ["cand-1"])
check("round trip: unknown id reads back as nothing", run(db.get_candidate("nope")), None)


# ---------- candidate insertion is idempotent

run(db.create_candidate(**make("cand-2", title="duplicate submission")))
before = run(db.get_candidate("cand-2"))
run(db.create_candidate(**make("cand-2", title="a different title on resubmission")))
after = run(db.get_candidate("cand-2"))
check("idempotent: resubmitting the same id does not duplicate the row",
      len(run(db.list_candidates(repo=REPO))), 2)
check("idempotent: the original fields win, not the resubmission",
      after["title"], before["title"])

run(db.transition_candidate("cand-2", "accepted"))
run(db.create_candidate(**make("cand-2", title="resubmitted after acceptance")))
check("idempotent: resubmitting after a transition does not reset status",
      run(db.get_candidate("cand-2"))["status"], "accepted")


# ---------- candidate transitions

run(db.create_candidate(**make("cand-3")))
accepted = run(db.transition_candidate("cand-3", "accepted"))
check("transitions: accepting a pending candidate returns the updated row",
      accepted["status"], "accepted")
check("transitions: the row itself moved",
      run(db.get_candidate("cand-3"))["status"], "accepted")

try:
    run(db.transition_candidate("cand-3", "rejected"))
    check("transitions: a second transition off an accepted candidate is rejected", False)
except ValueError:
    check("transitions: a second transition off an accepted candidate is rejected", True)

try:
    run(db.transition_candidate("cand-3", "accepted"))
    check("transitions: repeating the same terminal transition is rejected too", False)
except ValueError:
    check("transitions: repeating the same terminal transition is rejected too", True)

run(db.create_candidate(**make("cand-4")))
rejected = run(db.transition_candidate("cand-4", "rejected"))
check("transitions: a pending candidate can instead move to rejected", rejected["status"], "rejected")

try:
    run(db.transition_candidate("cand-4", "accepted"))
    check("transitions: a rejected candidate cannot flip to accepted", False)
except ValueError:
    check("transitions: a rejected candidate cannot flip to accepted", True)

run(db.create_candidate(**make("cand-5")))
try:
    run(db.transition_candidate("cand-5", "pending"))
    check("transitions: an invalid destination status is rejected", False)
except ValueError:
    check("transitions: an invalid destination status is rejected", True)
check("transitions: the rejected attempt left the candidate pending",
      run(db.get_candidate("cand-5"))["status"], "pending")

try:
    run(db.transition_candidate("does-not-exist", "accepted"))
    check("transitions: transitioning an unknown candidate raises", False)
except ValueError:
    check("transitions: transitioning an unknown candidate raises", True)


tmp.cleanup()
print()
print(f"{len(fails)} failed" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
