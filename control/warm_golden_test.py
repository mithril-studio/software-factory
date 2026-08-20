"""Warming a repo's golden: what gets restored, what gets captured, and what gets destroyed.

A provisioning run is the one run that spends a VM to produce something other than a pull
request, and the thing it produces is a snapshot every later run for that repo will boot. Three
ways that goes wrong quietly, and each is a section below:

1. **It captures the wrong machine, or under the wrong name.** A snapshot saved as anything
   other than `golden-<repo-slug>` is invisible to `resolve_snapshot` — no error, just a warm
   golden nobody boots and a bill for making it.
2. **It leaks the machine.** The VM exists only to be photographed. One that survives holds a
   slot out of an account that caps at 20, and holds it until the auto-destroy timer.
3. **It reports success it did not have.** A repo whose install failed must not end up marked
   `ready` with a snapshot behind it that has half a `node_modules` in it.

Nothing here talks to boxd. The stub records what was asked of it, which is what makes the
order assertable: restore, install, *then* capture, then destroy. A test that only checked the
end state could not tell a capture-before-install from a correct one.

The database is real, against a throwaway file, because the register's `provision_status` is
half of what this feature is — and stubbing it would mean asserting that the code calls a
function rather than that a repo ends up in the right state.

Run it directly, no framework needed:

    .venv/bin/python -m control.warm_golden_test
"""
import asyncio
import sys
import tempfile
from pathlib import Path

from control import agents, db, github, provision, repos, runner
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
object.__setattr__(settings, "repos", ())
settings.log_dir.mkdir(parents=True, exist_ok=True)
asyncio.run(db.init())

REPO = "acme/api"
SNAPSHOT = "golden-acme-api"
PROFILE = "# acme/api\n\n## Setup\n\nInstall with `pnpm install --frozen-lockfile`.\n"


# ---------- the stub fleet

class Thing:
    def __init__(self, id, name):
        self.id, self.name = id, name


class Chunk:
    def __init__(self, data, is_stderr=False):
        self.data, self.is_stderr = data.encode(), is_stderr


class Stream:
    """One exec session: replays canned output, then reports a status."""

    def __init__(self, chunks, code):
        self._chunks, self.exit_code = chunks, code

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def iter_chunks(self):
        for c in self._chunks:
            yield c


class Machines:
    def __init__(self, boxd):
        self._boxd = boxd

    async def list(self):
        """What `runner.headroom` counts against the boxd quota before provisioning."""
        return list(self._boxd.live)

    async def create(self, name=None, **kw):
        self._boxd.calls.append(("create", {"name": name, **kw}))
        machine = Thing("vm-1", name)
        self._boxd.live.append(machine)
        return machine

    async def fork(self, source, name=None, **kw):
        self._boxd.calls.append(("fork", {"source": source, "name": name}))
        return Thing("vm-forked", name)

    async def wait_until_ready(self, machine_id, timeout=None):
        self._boxd.calls.append(("wait_until_ready", {"id": machine_id}))

    async def delete(self, machine_id):
        self._boxd.calls.append(("machines.delete", {"id": machine_id}))
        self._boxd.destroyed.append(machine_id)
        self._boxd.live = [m for m in self._boxd.live if m.id != machine_id]

    def stream_exec(self, machine_id, *, command, env=None, close_stdin=False):
        self._boxd.calls.append(("stream_exec", {"id": machine_id, "command": command, "env": env}))
        return Stream(
            [Chunk("FACTORY: cloning acme/api\n"), Chunk("added 617 packages\n")],
            self._boxd.exit_code,
        )


class Snapshots:
    def __init__(self, boxd):
        self._boxd = boxd

    async def list(self, **kw):
        return [Thing(f"s-{i}", n) for i, n in enumerate(self._boxd.snapshot_names)]

    async def create(self, machine_id, name):
        self._boxd.calls.append(("snapshots.create", {"machine": machine_id, "name": name}))
        self._boxd.snapshot_names.append(name)
        return Thing("s-new", name)

    async def delete(self, name, **kw):
        self._boxd.calls.append(("snapshots.delete", {"name": name}))
        if name not in self._boxd.snapshot_names:
            raise RuntimeError(f"no such snapshot: {name}")
        self._boxd.snapshot_names.remove(name)


class FakeBoxd:
    def __init__(self, snapshots=(agents.BASE_SNAPSHOT,), exit_code=0):
        self.calls = []
        self.destroyed = []
        self.live = []
        self.snapshot_names = list(snapshots)
        self.exit_code = exit_code
        self.machines = Machines(self)
        self.snapshots = Snapshots(self)

    async def close(self):
        pass

    def named(self, call):
        found = [kw for name, kw in self.calls if name == call]
        return found[0] if found else None

    def order(self):
        return [name for name, _ in self.calls]


def warm(boxd, profile=PROFILE, branch="main"):
    """Run one provisioning to completion against a stubbed fleet and GitHub."""
    real = (runner.client, github.default_branch, github.file)
    runner.client = lambda: boxd
    github.default_branch = lambda repo: _async(branch)
    github.file = lambda repo, path, ref=None: _async(profile)
    try:
        run_id = asyncio.run(provision.create(REPO))
        # `create` schedules the work on a task in the loop it ran in, and that loop is gone.
        # Run the body directly instead, which is what the task would have awaited.
        asyncio.run(
            provision._guarded(run_id, REPO, branch, provision.preflight.setup_command(profile))
        )
        return run_id
    finally:
        runner.client, github.default_branch, github.file = real
        agents.forget()


async def _async(value):
    return value


asyncio.run(repos.add(REPO))


# ---------- the happy path, in order

boxd = FakeBoxd()
run_id = warm(boxd)
run = asyncio.run(db.get_run(run_id))

check("warm: it restores from the base image, never from the repo's own golden",
      (boxd.named("create") or {}).get("from_snapshot"), agents.BASE_SNAPSHOT)
check("warm: on the machine-restoring API, not the fork one", boxd.named("fork"), None)
check("warm: the VM is named with the prefix reconcile sweeps on",
      (boxd.named("create") or {}).get("name", "").startswith(runner.PROVISION_PREFIX), True)
check("warm: and reconcile recognises it as ours",
      runner.is_run_vm((boxd.named("create") or {}).get("name", "")), True)
check("warm: which the fleet view calls a provisioning machine",
      runner.vm_role((boxd.named("create") or {}).get("name", "")), "provision")

check("warm: it captures the snapshot the repo's runs will resolve onto",
      (boxd.named("snapshots.create") or {}).get("name"), SNAPSHOT)
check("warm: from the machine it just installed into",
      (boxd.named("snapshots.create") or {}).get("machine"), "vm-1")
check("warm: install happens before the capture, not after",
      boxd.order().index("stream_exec") < boxd.order().index("snapshots.create"), True)
check("warm: and the capture before the machine is destroyed",
      boxd.order().index("snapshots.create") < boxd.order().index("machines.delete"), True)
check("warm: the machine does not survive the run", boxd.destroyed, ["vm-1"])

check("warm: the run is recorded as one", (run["kind"], run["status"]), ("provision", "succeeded"))
check("warm: against the snapshot it produced", run["golden"], SNAPSHOT)
check("warm: with no issue behind it, because there is none", run["issue_number"], 0)
check("warm: the register now points the repo at its golden",
      (repos.rows()[0]["golden"], repos.rows()[0]["provision_status"]), (SNAPSHOT, provision.READY))
check("warm: and a dispatch would boot it rather than the base",
      agents.resolve_snapshot(REPO, boxd.snapshot_names), SNAPSHOT)

# The install command is the repo's own, handed over as a value rather than pasted into the
# script — the SDK shell-quotes what it puts in the exec prefix, so `eval` runs exactly it.
env = (boxd.named("stream_exec") or {}).get("env") or {}
check("warm: the setup command comes from the repo's .factory.md",
      env.get("FACTORY_SETUP"), "pnpm install --frozen-lockfile")
check("warm: the script evaluates it rather than embedding it",
      'eval "$FACTORY_SETUP"' in provision.SCRIPT, True)
check("warm: and the command itself is nowhere in the script text",
      "pnpm" in provision.SCRIPT, False)
check("warm: the VM is told which repo to clone", env.get("FACTORY_REPO"), REPO)
check("warm: and which branch to install from", env.get("FACTORY_BASE"), "main")
# The pre-clone override points at whatever repo an old golden carried. Honouring it here
# would install one repo's dependencies and capture the result as another repo's golden.
check("warm: the pre-clone checkout override is switched off", env.get("FACTORY_REPO_DIR"), "")
STEPS = ("gh repo clone", "git checkout -B", ".nvmrc", 'eval "$FACTORY_SETUP"')
check("warm: the script clones, checks out, guards the toolchain, then installs — in that order",
      [provision.SCRIPT.index(step) for step in STEPS],
      sorted(provision.SCRIPT.index(step) for step in STEPS))

log_text = Path(run["log_path"]).read_text()
check("warm: the VM's output is in the run log the UI tails",
      "added 617 packages" in log_text, True)


# ---------- a failed install captures nothing

boxd = FakeBoxd(exit_code=94)
run_id = warm(boxd)
run = asyncio.run(db.get_run(run_id))

check("failed: nothing is captured when the setup command fails",
      boxd.named("snapshots.create"), None)
check("failed: the machine is still destroyed", boxd.destroyed, ["vm-1"])
check("failed: the run says so", run["status"], "failed")
check("failed: and names the exit status", "94" in (run["error"] or ""), True)
check("failed: the register records the failure rather than a golden",
      (repos.rows()[0]["golden"], repos.rows()[0]["provision_status"]),
      (None, provision.FAILED))
check("failed: so the repo falls back to the base and installs for itself",
      agents.resolve_snapshot(REPO, boxd.snapshot_names), agents.BASE_SNAPSHOT)


# ---------- a repo that names no setup command is refused before anything is spent

boxd = FakeBoxd()
real = (runner.client, github.default_branch, github.file)
runner.client = lambda: boxd
github.default_branch = lambda repo: _async("main")
github.file = lambda repo, path, ref=None: _async("# acme/api\n\nNo setup section here.\n")
try:
    asyncio.run(provision.create(REPO))
    check("no setup: a repo that names no install command is refused", False)
except ValueError as exc:
    check("no setup: a repo that names no install command is refused", True)
    check("no setup: and the refusal says what to add",
          "## Setup" in str(exc) and runner.PROFILE_PATH in str(exc), True)
finally:
    runner.client, github.default_branch, github.file = real
check("no setup: no VM was created to find that out", boxd.calls, [])
check("no setup: and no run row was written for it",
      [r for r in asyncio.run(db.list_runs()) if r["status"] == "queued"], [])


# ---------- deleting a golden

boxd = FakeBoxd(snapshots=[agents.BASE_SNAPSHOT, SNAPSHOT])
real_client = runner.client
runner.client = lambda: boxd
try:
    check("delete: removing a repo's golden reports that one went",
          asyncio.run(provision.unprovision(REPO)), True)
    check("delete: and it is gone from the fleet", SNAPSHOT in boxd.snapshot_names, False)
    check("delete: the base is untouched", agents.BASE_SNAPSHOT in boxd.snapshot_names, True)
    check("delete: the register goes back to the cold path",
          repos.rows()[0]["provision_status"], provision.NONE)
    check("delete: deleting one that is not there is False, not an error",
          asyncio.run(provision.unprovision(REPO)), False)
finally:
    runner.client = real_client
    agents.forget()

# `golden_name` falls back to the base for a repo with no nameable slug. Deleting *that* would
# take out the image every repo in the deployment boots.
try:
    asyncio.run(provision.unprovision("///"))
    check("delete: a repo that resolves to the base image is refused", False)
except ValueError as exc:
    check("delete: a repo that resolves to the base image is refused",
          agents.BASE_SNAPSHOT in str(exc), True)


# ---------- what the runs prove about the golden they booted
#
# The warm tier's other failure, and the one that went unread for three hours: a golden that
# exists, is restorable, resolves fine — and has never once worked. `goldens._loop` warned
# `no run has yet proved: <name>` every five minutes while `resolve_snapshot` went on handing
# it to every dispatch, because dispatch asked the *name* and the evidence sat in the runs
# table with nothing reading it.
#
# Against the real database, because the whole feature is "the ledger decides", and stubbing
# the ledger would assert that a function gets called rather than that a repo boots the right
# image. Timestamps are explicit and far in the future so these rows sort after the
# provisioning runs above rather than racing them.

FLEET = (agents.BASE_SNAPSHOT, SNAPSHOT)


def finished(run_id, status, at, **fields):
    """One finished run on the warm golden, straight into the table."""
    asyncio.run(db.create_run(
        id=run_id, repo=REPO, issue_number=0, golden=SNAPSHOT, status=status,
        created_at=at, finished_at=at, **fields,
    ))
    return asyncio.run(db.snapshot_evidence())


# A stall is a failure like any other as far as the ledger is concerned — the point of naming
# it in `runs.error` is that a human reading the row learns which of the four endings it was.
seen = finished("r-stall", "failed", "2099-01-01T00:00:00Z",
                error="stalled: no output for 2700s after 14 lines")
check("evidence: a failed run is the golden's last verdict",
      seen[SNAPSHOT]["last_verdict"], "failed")
check("evidence: and the run is named, so a skip can be traced back to it",
      seen[SNAPSHOT]["verdict_run"], "r-stall")
check("evidence: nothing has produced usage on it, so nothing has proved it",
      seen[SNAPSHOT].get("verified_at"), None)
check("dispatch: so the repo falls back to the base and installs for itself",
      agents.resolve_snapshot(REPO, FLEET, seen), agents.BASE_SNAPSHOT)

# A human stopping a run is a decision about the work, not a finding about the image. It lands
# in `last_run` — which is what the fleet page shows — but it must not become the verdict the
# demotion reads, or a cancel would hide the failure behind it and change where runs boot.
seen = finished("r-cancelled", "cancelled", "2099-01-02T00:00:00Z")
check("evidence: a cancellation is the most recent run",
      seen[SNAPSHOT]["last_run"], "r-cancelled")
check("evidence: but it is not a verdict, so the failure behind it still stands",
      (seen[SNAPSHOT]["last_verdict"], seen[SNAPSHOT]["verdict_run"]), ("failed", "r-stall"))
check("dispatch: and it changes nothing about where the run boots",
      agents.resolve_snapshot(REPO, FLEET, seen), agents.BASE_SNAPSHOT)

# The quarantine reverses itself. Nothing was deleted and no register was rewritten, so one
# run that authenticates on the snapshot is enough to start booting it again — which is also
# what makes the rule safe to apply on a single failure.
seen = finished("r-good", "succeeded", "2099-01-03T00:00:00Z", tokens_out=1200, cost_usd=0.42)
check("evidence: a run that produced usage proves the image's credentials still work",
      bool(seen[SNAPSHOT]["verified_at"]), True)
check("dispatch: so the warm golden is booted again, with nothing to re-warm",
      agents.resolve_snapshot(REPO, FLEET, seen), SNAPSHOT)

# The two ways of knowing nothing, at the level a dispatch actually asks the question. An
# unreadable ledger answers `{}` and a first-ever golden has no row; both must boot the warm
# golden. Demoting on a measurement that was never taken is the mistake this all came from.
check("dispatch: an evidence read that failed is unknown, and unknown boots the golden",
      agents.resolve_snapshot(REPO, FLEET, {}), SNAPSHOT)
check("dispatch: and a caller that passes none gets today's behaviour",
      agents.resolve_snapshot(REPO, FLEET), SNAPSHOT)


tmp.cleanup()
print()
print(f"{len(fails)} failed" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
