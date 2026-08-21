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
4. **It destroys the machine while boxd is still writing the capture.** `snapshots.create`
   returns once the capture is *queued*; reaping then leaves the name `pending` forever with
   no version behind it, which `agents.available()` refuses to dispatch onto and nothing can
   repair. The stub below models that delay deliberately — a stub that publishes the finished
   snapshot synchronously cannot express the bug at all, which is why this test passed for as
   long as the bug was live.

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
# No real waiting: the wait is asserted by counting fleet listings, not by burning seconds.
object.__setattr__(settings, "capture_poll", 0.0)
object.__setattr__(settings, "capture_timeout", 30)
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


class Snap:
    """A snapshot as the fleet reports one: a name, the newest *ready* version, a status.

    `version` is the load-bearing field. boxd answers with the name the moment a capture is
    queued and fills the version in only when the capture has been written, which is the whole
    reason a warm-up cannot reap its machine as soon as `create` returns.
    """

    def __init__(self, name, version="v1", status="ready"):
        self.name, self.version, self.status = name, version, status
        # `runner._is_snapshot` matches on either, so the stub carries both.
        self.id = f"snap-{name}"


class Snapshots:
    """A fleet where a capture takes `capture_ticks` listings to land, or never lands.

    The stub used to make `create` publish the finished snapshot synchronously, which is the
    one thing boxd does not do — so the test asserted that `snapshots.create` was called
    before `machines.delete` (true, and not the question) while production destroyed the
    machine underneath a capture that had only been queued.
    """

    def __init__(self, boxd):
        self._boxd = boxd

    async def list(self, **kw):
        self._boxd.listings += 1
        for name, pending in list(self._boxd.capturing.items()):
            pending["ticks"] -= 1
            if pending["ticks"] <= 0 and pending["lands"]:
                self._boxd.snaps[name] = Snap(name, pending["version"])
                del self._boxd.capturing[name]
        rows = [Snap(n, s.version, s.status) for n, s in self._boxd.snaps.items()]
        rows += [Snap(n, "", "pending") for n in self._boxd.capturing]
        return rows

    async def create(self, machine_id, name):
        self._boxd.calls.append(("snapshots.create", {"machine": machine_id, "name": name}))
        was = self._boxd.snaps.get(name)
        self._boxd.capturing[name] = {
            "ticks": self._boxd.capture_ticks,
            "lands": self._boxd.capture_lands,
            # A re-save bumps the version. The wait has to notice *that*, not merely that a
            # version exists — it already did, from the previous build.
            "version": f"v{int((was.version or 'v0')[1:]) + 1}" if was else "v1",
        }
        return Snap(name, "", "pending")

    async def delete(self, name, **kw):
        self._boxd.calls.append(("snapshots.delete", {"name": name}))
        gone = self._boxd.capturing.pop(name, None)
        if self._boxd.snaps.pop(name, None) is None and gone is None:
            raise RuntimeError(f"no such snapshot: {name}")


class FakeBoxd:
    def __init__(self, snapshots=(agents.BASE_SNAPSHOT,), exit_code=0,
                 capture_ticks=2, capture_lands=True):
        self.calls = []
        self.destroyed = []
        self.live = []
        self.snaps = {n: Snap(n) for n in snapshots}
        # Names whose capture has been queued but not yet written.
        self.capturing = {}
        self.listings = 0
        self.capture_ticks = capture_ticks
        self.capture_lands = capture_lands
        self.exit_code = exit_code
        self.machines = Machines(self)
        self.snapshots = Snapshots(self)

    @property
    def snapshot_names(self):
        """Restorable names — what `resolve_snapshot` would actually be given."""
        return list(self.snaps)

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


# ---------- the capture is waited out, not assumed
#
# The bug this section exists for: boxd answers `create` when the capture is queued, and the
# machine used to be destroyed on that answer. Every check here is about ordering against the
# *version appearing*, which is the only evidence a capture actually landed.

boxd = FakeBoxd(capture_ticks=3)
run_id = warm(boxd)
run = asyncio.run(db.get_run(run_id))

check("capture: the run waits for the fleet to publish a version",
      boxd.listings >= 3, True)
check("capture: nothing is still mid-capture when the run finishes", boxd.capturing, {})
check("capture: and the snapshot is restorable, not merely named",
      boxd.snaps[SNAPSHOT].version, "v1")
check("capture: the machine is destroyed only after that",
      boxd.destroyed, ["vm-1"])
check("capture: the run succeeded", run["status"], "succeeded")
check("capture: the log names the version that landed",
      "v1 saved" in Path(run["log_path"]).read_text(), True)


# ---------- a rebuild waits for the version to *change*
#
# A re-save publishes a new version under a name that already had one, so "does it have a
# version" is true before the capture starts. Testing that instead would return instantly and
# reap the machine exactly as the original bug did.

boxd = FakeBoxd(snapshots=[agents.BASE_SNAPSHOT, SNAPSHOT], capture_ticks=2)
run_id = warm(boxd)
run = asyncio.run(db.get_run(run_id))

check("rebuild: it waits past the version the golden already had",
      boxd.snaps[SNAPSHOT].version, "v2")
check("rebuild: the run succeeded", run["status"], "succeeded")
check("rebuild: the register points at the rebuilt golden",
      (repos.rows()[0]["golden"], repos.rows()[0]["provision_status"]),
      (SNAPSHOT, provision.READY))


# ---------- a capture that never lands is deleted rather than left in the fleet

boxd = FakeBoxd(capture_lands=False)
run_id = warm(boxd)
run = asyncio.run(db.get_run(run_id))

check("stuck: the run fails rather than reporting a golden nothing can boot",
      run["status"], "failed")
check("stuck: the half-written name is deleted",
      (boxd.named("snapshots.delete") or {}).get("name"), SNAPSHOT)
check("stuck: so the fleet holds no unrestorable golden",
      SNAPSHOT in boxd.snapshot_names, False)
check("stuck: the machine is still reaped", boxd.destroyed, ["vm-1"])
check("stuck: and the repo falls back to the base",
      agents.resolve_snapshot(REPO, boxd.snapshot_names), agents.BASE_SNAPSHOT)


# ---------- a failed rebuild leaves the golden that is still there alone
#
# Provisioning always rebuilds from the base, so a failed re-provision does not touch the
# previous snapshot — `resolve_snapshot` keeps resolving onto it. Blanking the register would
# make the Projects page report "no golden" for a repo whose runs are booting one.

boxd = FakeBoxd(snapshots=[agents.BASE_SNAPSHOT, SNAPSHOT], exit_code=94)
run_id = warm(boxd)
run = asyncio.run(db.get_run(run_id))

check("survivor: the rebuild failed", run["status"], "failed")
check("survivor: the previous golden is untouched", SNAPSHOT in boxd.snapshot_names, True)
check("survivor: so the register keeps pointing at it",
      (repos.rows()[0]["golden"], repos.rows()[0]["provision_status"]),
      (SNAPSHOT, provision.READY))
check("survivor: and a dispatch still boots it",
      agents.resolve_snapshot(REPO, boxd.snapshot_names), SNAPSHOT)


# ---------- a repo that names no setup command still gets a golden, without the install
#
# The clone is the half the control plane can always do without guessing, and it is worth
# minutes on its own. What it must never do is infer an install command from a lock file.

NO_SETUP = "# acme/api\n\nNo setup section here.\n"
boxd = FakeBoxd()
run_id = warm(boxd, profile=NO_SETUP)
run = asyncio.run(db.get_run(run_id))
env = (boxd.named("stream_exec") or {}).get("env") or {}

check("clone-only: the run is not refused", run["status"], "succeeded")
check("clone-only: a golden is captured anyway",
      (boxd.named("snapshots.create") or {}).get("name"), SNAPSHOT)
check("clone-only: with no setup command to run", env.get("FACTORY_SETUP"), "")
check("clone-only: the script skips the install rather than running an empty one",
      'if [ -n "$FACTORY_SETUP" ]; then' in provision.SCRIPT, True)
# The stub replays canned output rather than running the script, so what the VM would print
# is asserted against the script text — the same place the install command is checked.
check("clone-only: and the script says why it skipped, in the log the UI tails",
      "capturing the clone without installing" in provision.SCRIPT, True)
check("clone-only: and says what to add to get the install baked in too",
      "## Setup" in provision.SCRIPT, True)
check("clone-only: the register reports it warm",
      (repos.rows()[0]["golden"], repos.rows()[0]["provision_status"]),
      (SNAPSHOT, provision.READY))


# ---------- a setup command spanning lines is still refused
#
# That is a malformed profile, not an absent one, and running it would be running something
# nobody wrote.

boxd = FakeBoxd()
real = (runner.client, github.default_branch, github.file)
runner.client = lambda: boxd
github.default_branch = lambda repo: _async("main")
github.file = lambda repo, path, ref=None: _async(PROFILE)
real_setup = provision.setup_command
provision.setup_command = lambda repo, base: _async("npm ci\nnpm run build")
try:
    asyncio.run(provision.create(REPO))
    check("multiline: a setup command spanning lines is refused", False)
except ValueError as exc:
    check("multiline: a setup command spanning lines is refused",
          "more than one line" in str(exc), True)
finally:
    runner.client, github.default_branch, github.file = real
    provision.setup_command = real_setup
check("multiline: no VM was created to find that out", boxd.calls, [])


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


tmp.cleanup()
print()
print(f"{len(fails)} failed" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
