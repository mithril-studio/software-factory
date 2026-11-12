"""One run end to end, with boxd and GitHub faked and everything else real.

This is the whole product in one file: an issue is claimed, a VM is made, the agent streams,
a pull request is found, the issue is told, and the VM is destroyed — on every path,
including the ones that crash. The database and the telemetry store are real throwaway files,
so what the issue comment says is what the run row stored.

Run it directly, no framework needed:

    .venv/bin/python -m control.runner_test
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

from telemetry import config as telemetry_config
from telemetry import store as telemetry_store

from control import agents, db, github, poller, repos, runner
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
object.__setattr__(settings, "max_machines", 0)
object.__setattr__(settings, "base_url", "https://factory.test")
settings.log_dir.mkdir()
telemetry_config.db_path = Path(tmp.name) / "factory.db"
asyncio.run(db.init())
asyncio.run(telemetry_store.init())
asyncio.run(repos.add("acme/api"))
REPO = "acme/api"


# ---------- fakes

class Machine:
    def __init__(self, id, name):
        self.id, self.name = id, name


class Chunk:
    def __init__(self, text):
        self.data, self.is_stderr = text.encode(), False


class Stream:
    def __init__(self, text, exit_code, hang):
        self.text, self.exit_code, self.hang = text, exit_code, hang

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def iter_chunks(self):
        yield Chunk(self.text)
        if self.hang:
            await asyncio.sleep(3600)


class ExecResult:
    stdout = ""


class FakeBoxd:
    """Records what was created, what ran, and what was destroyed."""

    instances: list = []

    def __init__(self, stdout="", exit_code=0, ready_error=None, hang=False):
        self.stdout, self.exit_code, self.ready_error, self.hang = stdout, exit_code, ready_error, hang
        self.machines = self
        self.snapshots = self
        self.created, self.deleted, self.env = [], [], None

    # snapshots
    async def list(self):
        return [Machine("s-copy", agents.BASE_SNAPSHOT)]

    # machines
    async def create(self, name, from_snapshot, **kw):
        self.created.append((name, from_snapshot))
        return Machine(f"id-{name}", name)

    async def wait_until_ready(self, machine_id, timeout):
        if self.ready_error:
            raise self.ready_error

    def stream_exec(self, machine_id, command, env, close_stdin):
        self.env = env
        return Stream(self.stdout, self.exit_code, self.hang)

    async def exec(self, machine_id, script, timeout):
        return ExecResult()

    async def delete(self, machine_id):
        self.deleted.append(machine_id)

    async def close(self):
        pass


class FakeGitHub:
    """`before` is the branch's newest PR when the run starts (or None); `after` is the PR
    the agent leaves open. `tip` is the remote branch's head commit."""

    def __init__(self, before=None, after="https://github.com/acme/api/pull/9", tip="abc123",
                 draft=False, lookup_error=None, label_error=None):
        self.before, self.after, self.tip, self.draft = before, after, tip, draft
        self.lookup_error, self.label_error = lookup_error, label_error
        self.calls, self.comments = [], []
        self.agent_done = False

    async def get_issue(self, repo, number):
        return {"number": number, "title": "Add a thing", "body": "Do it.", "labels": []}

    async def remove_label(self, repo, number, label):
        if self.label_error and label == "agent:queued":
            raise self.label_error
        self.calls.append(("remove", label))

    async def add_labels(self, repo, number, labels):
        self.calls.append(("add", labels[0]))

    async def add_comment(self, repo, number, body):
        self.comments.append(body)

    async def latest_pr(self, repo, branch):
        if self.lookup_error:
            raise self.lookup_error
        if self.agent_done:
            if not self.after:
                return self.before
            return {"url": self.after, "state": "open", "draft": self.draft, "head_sha": "new"}
        return self.before

    async def branch_sha(self, repo, branch):
        return self.tip

    async def ref_sha(self, repo, ref):
        return "abc123"

    async def file(self, repo, path, ref):
        return None


def events(cost=1.25):
    lines = [
        'FACTORY-MANIFEST {"agent": "claude", "events": "claude-code"}',
        json.dumps({"type": "system", "subtype": "init", "session_id": "s1"}),
        json.dumps({"type": "result", "subtype": "success", "num_turns": 4,
                    "total_cost_usd": cost, "usage": {"input_tokens": 100, "output_tokens": 20}}),
    ]
    return "\n".join(lines) + "\n"


GH_NAMES = ("get_issue", "remove_label", "add_labels", "add_comment", "latest_pr", "branch_sha",
            "ref_sha", "file")


def run_one(boxd, gh, *, cancel_after=None, slots=None):
    """Start a run for #7 and wait for it to finish. Returns the final run row.

    `slots` pre-fills the concurrency semaphore so the run waits for one."""
    real_gh = {n: getattr(github, n) for n in GH_NAMES}
    real_client = runner.client
    for n in GH_NAMES:
        setattr(github, n, getattr(gh, n))
    runner.client = lambda: boxd
    agents.forget()

    # The agent "opens" its PR while it runs: find_pr starts answering once the stream began.
    real_stream = runner._stream

    async def stream(*a, **kw):
        try:
            return await real_stream(*a, **kw)
        finally:
            gh.agent_done = True

    runner._stream = stream

    async def go():
        if slots is not None:
            runner._semaphore = asyncio.Semaphore(slots)
        run_id = await runner.create(REPO, 7)
        if cancel_after is not None:
            await asyncio.sleep(cancel_after)
            await runner.cancel(run_id)
        task = runner._tasks.get(run_id)
        if task:
            try:
                await task
            except asyncio.CancelledError:
                pass
        return await db.get_run(run_id)

    try:
        return asyncio.run(go())
    finally:
        for n, f in real_gh.items():
            setattr(github, n, f)
        runner.client = real_client
        runner._stream = real_stream
        runner._semaphore = None


# ---------- the happy path
boxd, gh = FakeBoxd(stdout=events()), FakeGitHub()
row = run_one(boxd, gh)
check("happy: the run succeeds", row["status"], "succeeded")
check("happy: with the PR the agent opened", row["pr_url"], "https://github.com/acme/api/pull/9")
check("happy: and the cost the runtime reported", row["cost_usd"], 1.25)
check("happy: under budget", row["over_budget"], 0)
check("happy: agent:queued is taken off before anything else", gh.calls[0], ("remove", "agent:queued"))
check("happy: the issue says it is running", ("add", "agent:running") in gh.calls)
check("happy: and ends as done", gh.calls[-1], ("add", "agent:done"))
check("happy: running is cleared at the end", ("remove", "agent:running") in gh.calls)
check("happy: the first comment says where the PR will go", "pull request into `test`" in gh.comments[0])
check("happy: and links the live log", f"https://factory.test/runs/{row['id']}" in gh.comments[0])
check("happy: the closing comment names the PR", "https://github.com/acme/api/pull/9" in gh.comments[-1])
check("happy: and the cost", "$1.25" in gh.comments[-1])
check("happy: the VM was booted from the base golden", boxd.created[0][1], agents.BASE_SNAPSHOT)
check("happy: under the name the row claimed", boxd.created[0][0], row["vm_name"])
check("happy: and destroyed", boxd.deleted, [f"id-{row['vm_name']}"])
check("happy: a first run starts its branch fresh (no PR ever)", boxd.env["FACTORY_RESUME"], "1")
check("happy: the agent was told to open a PR into test",
      "gh pr create --base test" in boxd.env["FACTORY_PROMPT"])
check("happy: the agent recorded is what the golden announced", row["agent"], "claude")
check("happy: the repo is free again", asyncio.run(db.has_active_run(REPO)), False)

# ---------- no PR
boxd, gh = FakeBoxd(stdout=events()), FakeGitHub(after=None)
row = run_one(boxd, gh)
check("no PR: exit 0 without a pull request is a failure", (row["status"], row["error"]),
      ("failed", "no pull request opened"))
check("no PR: the issue is labelled failed", gh.calls[-1], ("add", "agent:failed"))
check("no PR: and told how to retry", "Re-add `agent:queued`" in gh.comments[-1])
check("no PR: the VM is still destroyed", len(boxd.deleted), 1)

# ---------- the agent fails
boxd, gh = FakeBoxd(stdout=events(), exit_code=1), FakeGitHub()
row = run_one(boxd, gh)
check("exit 1: is a failure even with a PR", (row["status"], row["error"]), ("failed", "agent exited 1"))
check("exit 1: the PR so far is still reported", "Pull request so far" in gh.comments[-1])

# ---------- a stream that never says how it ended
boxd, gh = FakeBoxd(stdout=events(), exit_code=None), FakeGitHub()
row = run_one(boxd, gh)
check("no exit code: is never read as success", (row["status"], row["exit_code"]), ("failed", -1))

# ---------- over budget (soft)
boxd, gh = FakeBoxd(stdout=events(cost=150.0)), FakeGitHub()
row = run_one(boxd, gh)
check("budget: a run past the budget still finishes", row["status"], "succeeded")
check("budget: but is flagged", row["over_budget"], 1)
check("budget: and the closing comment says so", "over the €100 run budget" in gh.comments[-1])

# ---------- resume decisions
OPEN = {"url": "https://github.com/acme/api/pull/3", "state": "open", "draft": False, "head_sha": "abc123"}
CLOSED = {"url": "https://github.com/acme/api/pull/2", "state": "closed", "draft": False, "head_sha": "abc123"}

boxd, gh = FakeBoxd(stdout=events()), FakeGitHub(before=OPEN)
run_one(boxd, gh)
check("resume: an open PR is continued", boxd.env["FACTORY_RESUME"], "1")
check("resume: the agent is told to read its comments",
      "gh pr view https://github.com/acme/api/pull/3 --comments" in boxd.env["FACTORY_PROMPT"])
check("resume: and to trust only collaborators", "authorAssociation" in boxd.env["FACTORY_PROMPT"])
check("resume: the start comment says which PR", "Continuing https://github.com/acme/api/pull/3" in gh.comments[0])

boxd, gh = FakeBoxd(stdout=events()), FakeGitHub(before=CLOSED, tip="abc123")
run_one(boxd, gh)
check("resume: a branch that is exactly what a closed PR left starts fresh", boxd.env["FACTORY_RESUME"], "0")

boxd, gh = FakeBoxd(stdout=events()), FakeGitHub(before=CLOSED, tip="pushed-since")
run_one(boxd, gh)
check("resume: commits pushed after the closed PR are continued, never deleted",
      boxd.env["FACTORY_RESUME"], "1")

boxd, gh = FakeBoxd(stdout=events()), FakeGitHub(before=None, tip="earlier-work")
run_one(boxd, gh)
check("resume: a branch that never had a PR is continued", boxd.env["FACTORY_RESUME"], "1")

boxd, gh = FakeBoxd(stdout=events()), FakeGitHub(lookup_error=RuntimeError("502"))
row = run_one(boxd, gh)
check("resume: a GitHub error deciding it fails the run", row["status"], "failed")
check("resume: before any VM exists, so nothing is deleted on a guess", boxd.created, [])

# ---------- a blocked agent
boxd, gh = FakeBoxd(stdout=events()), FakeGitHub(draft=True)
row = run_one(boxd, gh)
check("draft: a draft PR is not success", row["status"], "failed")
check("draft: it says the agent was blocked", "draft" in row["error"])
check("draft: and still links the PR", row["pr_url"], "https://github.com/acme/api/pull/9")
check("draft: the issue is labelled failed", gh.calls[-1], ("add", "agent:failed"))

# ---------- a crash mid-provision
boxd, gh = FakeBoxd(stdout=events(), ready_error=TimeoutError("boot")), FakeGitHub()
row = run_one(boxd, gh)
check("crash: a VM that never boots fails the run", row["status"], "failed")
check("crash: with a reason", "timed out" in row["error"] or "crashed" in row["error"])
check("crash: and the half-made VM is destroyed", len(boxd.deleted), 1)
check("crash: the issue is told", gh.calls[-1], ("add", "agent:failed"))

# ---------- cancel
boxd, gh = FakeBoxd(stdout=events(), hang=True), FakeGitHub()
row = run_one(boxd, gh, cancel_after=0.3)
check("cancel: the run ends cancelled", row["status"], "cancelled")
check("cancel: and its VM is destroyed", len(boxd.deleted), 1)
check("cancel: the issue is told", "cancelled" in gh.comments[-1])

# ---------- cancel while waiting for a slot
boxd, gh = FakeBoxd(stdout=events()), FakeGitHub()
row = run_one(boxd, gh, cancel_after=0.1, slots=0)
check("waiting: a run cancelled before it got a slot ends cancelled", row["status"], "cancelled")
check("waiting: agent:queued was already off, so it is not dispatched again",
      ("remove", "agent:queued") in gh.calls)
check("waiting: and no VM was ever made", boxd.created, [])

# ---------- the claim on GitHub fails
boxd, gh = FakeBoxd(stdout=events()), FakeGitHub(label_error=RuntimeError("403"))
real_gh = {n: getattr(github, n) for n in GH_NAMES}
for n in GH_NAMES:
    setattr(github, n, getattr(gh, n))
try:
    asyncio.run(runner.create(REPO, 7))
    check("claim: a label that cannot be removed refuses the run", False)
except RuntimeError:
    check("claim: a label that cannot be removed refuses the run", True)
finally:
    for n, f in real_gh.items():
        setattr(github, n, f)
check("claim: and the row is closed rather than left in flight",
      asyncio.run(db.has_active_run(REPO)), False)

# ---------- one run per repo
check("busy: the repo was free before", asyncio.run(db.has_active_run(REPO)), False)
asyncio.run(db.claim_run(id="held", repo=REPO, issue_number=1, status="running",
                         created_at=db.utcnow()))
real_get = github.get_issue
github.get_issue = FakeGitHub().get_issue
try:
    asyncio.run(runner.create(REPO, 8))
    check("busy: a second run for a busy repo is refused", False)
except runner.Busy:
    check("busy: a second run for a busy repo is refused", True)
finally:
    github.get_issue = real_get
asyncio.run(db.finish_run("held", "succeeded"))


# ---------- the poller picks the next issue
def next_issue(queued, heads, sequential=True):
    real = (github.list_issues_with_label, github.open_pr_heads)

    async def listed(repo, label):
        return [{"number": n, "title": ""} for n in queued]

    async def open_heads(repo, base, prefix=""):
        return heads

    github.list_issues_with_label, github.open_pr_heads = listed, open_heads
    object.__setattr__(settings, "sequential", sequential)
    try:
        return asyncio.run(poller.next_issue(REPO))
    finally:
        github.list_issues_with_label, github.open_pr_heads = real
        object.__setattr__(settings, "sequential", True)


check("poller: lowest queued issue first", next_issue([4, 9], []), 4)
check("poller: nothing queued, nothing to do", next_issue([], []), None)
check("poller: waits while another factory PR is open", next_issue([4], ["factory/issue-3"]), None)
check("poller: but an issue may continue its own open PR", next_issue([4], ["factory/issue-4"]), 4)
check("poller: independent issues do not wait", next_issue([4], ["factory/issue-3"], False), 4)

tmp.cleanup()
print()
print(f"{len(fails)} failed" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
