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
import inspect
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


# ---------- what may be written at all
#
# Candidates arrive from a JSONL file the agent wrote inside the VM, so the keys reaching
# `create_candidate` are not necessarily ones this schema has ever heard of. Column names are
# interpolated into the statement — SQL has no placeholder for an identifier — so an unknown
# key must be refused here rather than carried into the statement text.

try:
    run(db.create_candidate(**make("cand-6", note="not a column")))
    check("columns: an unknown column is refused", False)
except ValueError as exc:
    check("columns: an unknown column is refused", "note" in str(exc))
check("columns: the refused candidate was not inserted", run(db.get_candidate("cand-6")), None)

try:
    run(db.create_candidate(**{**make("cand-7"), '"); DROP TABLE memory_candidates; --': "x"}))
    check("columns: a column name carrying SQL is refused", False)
except ValueError:
    check("columns: a column name carrying SQL is refused", True)
check("columns: the table is still there",
      len(run(db.list_candidates(repo=REPO))) > 0, True)

try:
    run(db.create_candidate(**make("cand-8", status="accepted")))
    check("columns: a candidate cannot be born already accepted", False)
except ValueError:
    check("columns: a candidate cannot be born already accepted", True)
check("columns: nothing was inserted for it", run(db.get_candidate("cand-8")), None)



# ---------- collect a valid artifact (AC2)
#
# The artifact is a file the agent wrote inside the VM, read back out before the machine is
# destroyed. What arrives here is untrusted text: everything below is about what may cross
# that line, not about whether the learning is true.

from control import runner  # noqa: E402 - after the settings redirect above, deliberately


class Log:
    def __init__(self):
        self.lines = []

    def write(self, line):
        self.lines.append(line)


class StubResult:
    def __init__(self, stdout):
        self.stdout = stdout


class StubMachines:
    def __init__(self, stdout, raises=None):
        self.stdout = stdout
        self.raises = raises
        self.calls = 0

    async def exec(self, machine_id, script, timeout=None):
        self.calls += 1
        if self.raises:
            raise self.raises
        return StubResult(self.stdout)


class StubBoxd:
    def __init__(self, stdout, raises=None):
        self.machines = StubMachines(stdout, raises)


def collect(stdout, run_id="run-collect", raises=None):
    log = Log()
    boxd = StubBoxd(stdout, raises)
    queued = run(runner._collect_memory_candidates(boxd, "vm-1", run_id, REPO, log))
    return queued, log.lines


VALID = json.dumps({
    "domain": "repository",
    "type": "failure",
    "title": "aiosqlite rowcount is 0 after commit on a conditional UPDATE",
    "body": "Checking rowcount after commit reports 0 whatever the statement matched.",
    "resolution": "Read cursor.rowcount before commit.",
    "evidence": {"files": ["control/db.py"], "dirs": []},
    "confidence": "high",
})

queued, lines = collect(VALID + "\n")
check("collect valid artifact: one candidate queued", queued, 1)
stored = run(db.list_candidates(repo=REPO, status="pending"))
mine = [c for c in stored if c["run_id"] == "run-collect"]
check("collect valid artifact: it is attributed to the run that produced it", len(mine), 1)
check("collect valid artifact: and to that run's repository", mine[0]["repo"], REPO)
check("collect valid artifact: it lands pending, not accepted", mine[0]["status"], "pending")
check("collect valid artifact: the evidence survives as written",
      json.loads(mine[0]["evidence"])["files"], ["control/db.py"])
check("collect valid artifact: a failure keeps its resolution",
      "resolution" in json.loads(mine[0]["evidence"]), True)

# The id is derived from the run and the content, so a re-read of the same file — a retry of
# the collection, a run collected on both the ordinary and the exceptional path — is one
# candidate, not two.
collect(VALID + "\n")
check("collect valid artifact: collecting the same file twice queues it once",
      len([c for c in run(db.list_candidates(repo=REPO)) if c["run_id"] == "run-collect"]), 1)


# ---------- reject invalid artifacts (AC3)
#
# Every rejection is logged with a reason and none of them fails the run. A silent drop would
# read as "the agent proposed nothing", which is a different and much more comfortable fact
# than "the agent proposed something malformed".

def rejected(stdout, run_id):
    queued, lines = collect(stdout, run_id)
    return queued, [ln for ln in lines if "memory candidate rejected" in ln]


def one_bad(**overrides):
    base = json.loads(VALID)
    base.update(overrides)
    return json.dumps(base) + "\n"


cases = [
    ("not JSON at all", "this is just narration\n", "not JSON"),
    ("a JSON array rather than an object", "[1, 2, 3]\n", "not a JSON object"),
    ("a missing required field", one_bad(title=""), "missing title"),
    ("an unknown type", one_bad(type="anecdote"), "unknown type"),
    ("an unknown confidence", one_bad(confidence="certain"), "unknown confidence"),
    ("a failure with no resolution", one_bad(resolution=""), "no resolution"),
    ("an absolute evidence path",
     one_bad(evidence={"files": ["/etc/passwd"], "dirs": []}), "absolute"),
    ("an evidence path climbing out of the repo",
     one_bad(evidence={"files": ["../../../etc/shadow"], "dirs": []}), "climbs out"),
    ("evidence naming nothing at all",
     one_bad(evidence={"files": [], "dirs": []}), "names no file"),
    ("an oversized title", one_bad(title="x" * 500), "title is"),
    ("an oversized body", one_bad(body="x" * 9000), "body is"),
]
for i, (name, artifact, needle) in enumerate(cases):
    queued, why = rejected(artifact, f"run-bad-{i}")
    check(f"reject invalid artifacts: {name} is refused", queued, 0)
    check(f"reject invalid artifacts: {name} says why", any(needle in ln for ln in why), True)

# A run cannot turn its transcript into a queue: the whole file is refused past a byte budget,
# and the per-run record count is capped with the overflow logged rather than dropped quietly.
queued, why = rejected("x" * (runner.MEMORY_CANDIDATE_MAX_BYTES + 1) + "\n", "run-huge")
check("reject invalid artifacts: an oversized artifact queues nothing", queued, 0)
check("reject invalid artifacts: and says the whole file was refused",
      any("over the" in ln and "byte limit" in ln for ln in why), True)

many = "".join(one_bad(title=f"Learning number {n}") for n in range(30))
queued, why = rejected(many, "run-many")
check("reject invalid artifacts: the per-run cap holds",
      queued, runner.MEMORY_CANDIDATE_MAX_RECORDS)
check("reject invalid artifacts: the overflow is logged, not silently dropped",
      any("candidate limit" in ln for ln in why), True)

# One bad line does not cost the good ones beside it.
queued, why = rejected(VALID.replace("aiosqlite", "mixed batch") + "\nnot json\n", "run-mixed")
check("reject invalid artifacts: a good line survives a bad neighbour", queued, 1)

# Nothing about a proposal is worth a run: an unreachable VM is logged and swallowed.
queued, lines = collect(VALID, "run-boom", raises=RuntimeError("machine is gone"))
check("reject invalid artifacts: a failed read does not raise", queued, 0)
check("reject invalid artifacts: and is noted in the log",
      any("not collected" in ln for ln in lines), True)

# An empty or absent file is the ordinary case, and says nothing at all.
queued, lines = collect("", "run-empty")
check("reject invalid artifacts: no artifact means no candidates and no noise",
      (queued, lines), (0, []))


# ---------- collect before cleanup (AC4)
#
# The file lives inside the VM, so collection has exactly one moment: before the reap. Asserted
# against the source of `_execute_build` rather than by driving a whole run, because what is
# being pinned is the *order of two statements* on both the ordinary and the exceptional path —
# and a run that failed is the one whose learning is most likely to be worth keeping.

source = inspect.getsource(runner._execute)
collect_at = source.index("_collect_memory_candidates")
check("collect before cleanup: the ordinary path collects before it reaps",
      collect_at < source.index("await reap("), True)
check("collect before cleanup: it runs for a failed agent too, not only a successful one",
      collect_at < source.index("ok = exit_code == 0"), True)
check("collect before cleanup: the exceptional path collects before its fallback reap",
      source.rindex("_collect_memory_candidates") < source.rindex("await reap("), True)
check("collect before cleanup: and only once, when the ordinary path already ran",
      "not collected" in source, True)



# ---------- triage endpoints (#54)
#
# Operators decide; nothing here promotes a candidate into a repository's `.mem/`. Driven
# through the real ASGI app so the session gate is part of what is being tested — but without
# its lifespan, so no poller starts, no golden sweep runs, and nothing reaches GitHub or boxd.

from fastapi.testclient import TestClient  # noqa: E402

from control import app as app_module, auth, repos  # noqa: E402

client = TestClient(app_module.app)
SESSION = {auth.COOKIE_NAME: auth.issue_token()}

check("candidate read endpoints: an unauthenticated caller is refused",
      client.get("/api/memory/candidates").status_code, 401)

# Two repos, so "scoped to one repository" is a claim the data can falsify.
OTHER = "mithril-studio/other-project"
run(db.create_candidate(**make("cand-api-1")))
run(db.create_candidate(**make("cand-api-2")))
run(db.create_candidate(**make("cand-api-other", repo=OTHER)))

listed = client.get("/api/memory/candidates", params={"repo": REPO, "status": "pending"},
                    cookies=SESSION)
check("candidate read endpoints: an authenticated caller can list", listed.status_code, 200)
ids = {c["id"] for c in listed.json()}
check("candidate read endpoints: the repo's own pending candidates are listed",
      {"cand-api-1", "cand-api-2"} <= ids, True)
check("candidate read endpoints: another repo's candidate is not",
      "cand-api-other" in ids, False)
check("candidate read endpoints: every row carries its repo",
      {c["repo"] for c in listed.json()}, {REPO})

one = client.get("/api/memory/candidates/cand-api-1", cookies=SESSION)
check("candidate read endpoints: a single candidate can be fetched", one.status_code, 200)
check("candidate read endpoints: with its provenance",
      (one.json()["run_id"], one.json()["repo"]), ("run-1", REPO))
# Decoded, not handed back as a JSON string for every consumer to parse again.
check("candidate read endpoints: with its evidence as an object",
      one.json()["evidence"]["files"], ["control/db.py"])

# ---- transitions (AC2)

accepted = client.post("/api/memory/candidates/cand-api-1/accept", cookies=SESSION)
check("candidate transition endpoints: a pending candidate can be accepted",
      accepted.status_code, 200)
check("candidate transition endpoints: and says so", accepted.json()["status"], "accepted")
check("candidate transition endpoints: the stored row moved",
      run(db.get_candidate("cand-api-1"))["status"], "accepted")

rejected_resp = client.post("/api/memory/candidates/cand-api-2/reject", cookies=SESSION)
check("candidate transition endpoints: a pending candidate can be rejected",
      (rejected_resp.status_code, rejected_resp.json()["status"]), (200, "rejected"))

# ---- conflicts (AC3)

missing = client.get("/api/memory/candidates/nope", cookies=SESSION)
check("candidate endpoint conflicts: a missing candidate is a 404", missing.status_code, 404)
check("candidate endpoint conflicts: transitioning a missing candidate is a 404",
      client.post("/api/memory/candidates/nope/accept", cookies=SESSION).status_code, 404)

again = client.post("/api/memory/candidates/cand-api-1/accept", cookies=SESSION)
check("candidate endpoint conflicts: deciding twice is a 409, not a silent success",
      again.status_code, 409)
check("candidate endpoint conflicts: and the stored state is untouched",
      run(db.get_candidate("cand-api-1"))["status"], "accepted")
flip = client.post("/api/memory/candidates/cand-api-2/accept", cookies=SESSION)
check("candidate endpoint conflicts: a rejected candidate cannot be flipped to accepted",
      flip.status_code, 409)
check("candidate endpoint conflicts: and it stayed rejected",
      run(db.get_candidate("cand-api-2"))["status"], "rejected")
check("candidate endpoint conflicts: an invented decision is a 404",
      client.post("/api/memory/candidates/cand-api-1/maybe", cookies=SESSION).status_code, 404)

# ---- project counts (AC4)


async def _stub_source(*args, **kwargs):
    return "golden-copy"


class _StubBoxd:
    async def close(self):
        return None


real_client, real_source, real_rows = runner.client, runner.source_for, repos.rows
runner.client = lambda: _StubBoxd()
runner.source_for = _stub_source
repos.rows = lambda: [{"repo": REPO, "added_at": "2026-08-20T00:00:00Z"},
                      {"repo": OTHER, "added_at": "2026-08-20T00:00:00Z"}]
try:
    projects = {row["repo"]: row for row in client.get("/api/projects", cookies=SESSION).json()}
finally:
    runner.client, runner.source_for, repos.rows = real_client, real_source, real_rows

# `cand-5` is left pending by the transition checks above, and `cand-api-1`/`-2` were both
# decided — so this counts what is actually undecided rather than what was ever proposed.
expected = len([c for c in run(db.list_candidates(repo=REPO, status="pending"))])
check("project candidate counts: the repo reports its own pending queue",
      projects[REPO]["pending_candidates"], expected)
check("project candidate counts: the other repo reports its own, not this one's",
      projects[OTHER]["pending_candidates"], 1)
check("project candidate counts: and they are different numbers, so nothing is mixed",
      projects[REPO]["pending_candidates"] != projects[OTHER]["pending_candidates"], True)


tmp.cleanup()
print()
print(f"{len(fails)} failed" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
