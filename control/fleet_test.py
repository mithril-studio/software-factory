"""Not leaking machines: the reap, the headroom check, and the sweep.

The boxd account holds 20 concurrent machines. Nothing in this codebase counted against that
until now — `FACTORY_MAX_CONCURRENT` bounds how many runs execute at once and says nothing
about the goldens, the control plane, or a machine somebody left running by hand. So the ways
a slot goes missing are all quiet ones, and each has a section here:

1. **A run that crashes between provisioning and reaping.** The reap lived in the body of
   `_execute`, so a `wait_until_ready` timeout, a dropped stream or a run that hit
   `FACTORY_RUN_TIMEOUT` left the machine running until its two-hour self-destruct. Three of
   those and the factory cannot dispatch.
2. **A sweep that stops at the first machine that will not die.** `reconcile` deleted orphans
   in a bare loop; one raise aborted the rest. The sweep is what reclaims the quota, so a
   single stuck VM could keep every other orphan alive behind it.
3. **A sweep nobody runs.** `control/README.md` §4 described a periodic reconciler from the
   start. The function existed; nothing scheduled it. The only way a leaked VM was reclaimed
   was somebody noticing and pressing a button.

Nothing here talks to boxd. The database is real, against a throwaway file, because half of
what `reconcile` decides is "which runs does the table think are live".

Run it directly, no framework needed:

    .venv/bin/python -m control.fleet_test
"""
import asyncio
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
asyncio.run(db.init())


class Thing:
    def __init__(self, id, name):
        self.id, self.name = id, name


class Machines:
    def __init__(self, boxd):
        self._boxd = boxd

    async def list(self):
        return [Thing(f"id-{n}", n) for n in self._boxd.names]

    async def delete(self, machine_id):
        name = machine_id.removeprefix("id-")
        self._boxd.deletes.append(name)
        if name in self._boxd.undead:
            raise RuntimeError(f"{name} will not die")
        if name in self._boxd.names:
            self._boxd.names.remove(name)


class FakeBoxd:
    def __init__(self, names=(), undead=()):
        self.names = list(names)
        self.undead = set(undead)
        self.deletes = []
        self.machines = Machines(self)

    async def close(self):
        pass


class Log:
    def __init__(self):
        self.lines = []

    def write(self, line):
        self.lines.append(line)

    def said(self, text):
        return any(text in line for line in self.lines)


def run(coro):
    return asyncio.run(coro)


# ---------- reap: idempotent, never raises

log = Log()
boxd = FakeBoxd(["run-abc"])
run(runner.reap(boxd, Thing("id-run-abc", "run-abc"), log))
check("reap: destroys the machine", boxd.names, [])
check("reap: and says so in the run's own log", log.said("destroyed run-abc"), True)

log = Log()
run(runner.reap(boxd, None, log))
check("reap: nothing to reap is not an error", log.lines, [])

log = Log()
boxd = FakeBoxd(["run-keep"])
run(runner.reap(boxd, Thing("id-run-keep", "run-keep"), log, keep=True))
check("reap: keep leaves the machine alone", boxd.names, ["run-keep"])
check("reap: and says why it is still there",
      log.said("FACTORY_KEEP_FAILED"), True)

# A reap that raises would propagate out of a `finally` that is already handling the failure
# which brought it there, replacing the real error with this one.
log = Log()
boxd = FakeBoxd(["run-stuck"], undead=["run-stuck"])
run(runner.reap(boxd, Thing("id-run-stuck", "run-stuck"), log))
check("reap: a machine that refuses to die does not raise out of a finally", True)
check("reap: it is logged and handed to the reconciler",
      log.said("reconcile will sweep it"), True)


# ---------- headroom: refuse a full fleet, but sweep before giving up

object.__setattr__(settings, "max_machines", 4)

log = Log()
boxd = FakeBoxd(["golden-copy", "run-a"])
runner.client = lambda: boxd
run(runner.headroom(boxd, log))
check("headroom: room to spare is silent", log.lines, [])

# At the cap with an orphan in the fleet: sweep, then proceed. An orphaned run VM is exactly
# what the reconciler exists to reclaim, so stopping at the limit without trying would refuse
# a run over a slot we already own.
log = Log()
boxd = FakeBoxd(["golden-copy", "factory-control", "run-gone-1", "run-gone-2"])
runner.client = lambda: boxd
run(runner.headroom(boxd, log))
check("headroom: at the cap it sweeps", log.said("sweeping for orphans"), True)
check("headroom: reclaims the orphans", sorted(boxd.names), ["factory-control", "golden-copy"])
check("headroom: and proceeds", log.said("reclaimed to 2/4"), True)

# At the cap with nothing to reclaim: refuse, and say what the numbers are. Better than
# letting boxd refuse the create halfway through a dispatch.
log = Log()
boxd = FakeBoxd(["golden-copy", "factory-control", "someones-vm", "another"])
runner.client = lambda: boxd
try:
    run(runner.headroom(boxd, log))
    check("headroom: a full fleet with no orphans refuses", False)
except RuntimeError as exc:
    check("headroom: a full fleet with no orphans refuses", True)
    check("headroom: and the message carries the numbers", "4/4" in str(exc), True)
check("headroom: nothing that was not ours is touched", len(boxd.names), 4)

log = Log()
object.__setattr__(settings, "max_machines", 0)
boxd = FakeBoxd(["a", "b", "c", "d", "e"])
run(runner.headroom(boxd, log))
check("headroom: 0 switches the check off entirely", log.lines, [])
object.__setattr__(settings, "max_machines", 20)


# ---------- reconcile: one stuck machine must not abort the sweep

run(db.create_run(
    id="live", repo="acme/api", issue_number=1, status="running", vm_name="run-live",
    created_at=db.utcnow(),
))

boxd = FakeBoxd(
    ["golden-copy", "factory-control", "run-live", "run-orphan-1", "run-stubborn", "rev-orphan"],
    undead=["run-stubborn"],
)
runner.client = lambda: boxd
found = run(runner.reconcile())

check("reconcile: every orphan is attempted, not just the ones before the stuck one",
      sorted(boxd.deletes), ["rev-orphan", "run-orphan-1", "run-stubborn"])
check("reconcile: the ones that went are reported",
      sorted(found["destroyed"]), ["rev-orphan", "run-orphan-1"])
check("reconcile: and the one that would not is named rather than swallowed",
      found["stuck"], ["run-stubborn"])
check("reconcile: a VM with a live run behind it is left alone", "run-live" in boxd.names, True)
check("reconcile: and so is everything the factory did not create",
      [n for n in ("golden-copy", "factory-control") if n in boxd.names],
      ["golden-copy", "factory-control"])

# The other half: a run the table thinks is live whose VM has vanished.
run(db.create_run(
    id="ghost", repo="acme/api", issue_number=2, status="running", vm_name="run-vanished",
    created_at=db.utcnow(),
))
boxd = FakeBoxd(["golden-copy", "run-live"])
runner.client = lambda: boxd
found = run(runner.reconcile())
check("reconcile: a run whose VM is gone is failed rather than left running forever",
      found["stranded"], ["ghost"])
check("reconcile: with a reason somebody can read",
      run(db.get_run("ghost"))["error"], "VM disappeared while run was active")
check("reconcile: the run whose VM is still there is untouched",
      run(db.get_run("live"))["status"], "running")

# A run whose task is still in flight is never stranded, however slow boxd's listing is.
runner._tasks["ghost2"] = "pretend-task"
run(db.create_run(
    id="ghost2", repo="acme/api", issue_number=3, status="forking", vm_name="run-not-yet",
    created_at=db.utcnow(),
))
boxd = FakeBoxd(["golden-copy", "run-live"])
runner.client = lambda: boxd
found = run(runner.reconcile())
check("reconcile: a run still executing here is not stranded by a listing that lags",
      found["stranded"], [])
runner._tasks.pop("ghost2")


# ---------- the loop exists and is switched on by a setting

check("reconciler: it is scheduled, which is the whole point",
      callable(runner.start_reconciler), True)


async def loop_runs():
    object.__setattr__(settings, "reconcile_interval", 0)
    runner.start_reconciler()
    off = runner._reconciler is None
    object.__setattr__(settings, "reconcile_interval", 300)
    runner.start_reconciler()
    on = runner._reconciler is not None
    twice = runner._reconciler
    runner.start_reconciler()
    same = runner._reconciler is twice
    await runner.stop_reconciler()
    return off, on, same, runner._reconciler is None


off, on, same, stopped = asyncio.run(loop_runs())
check("reconciler: an interval of 0 switches it off", off, True)
check("reconciler: otherwise it starts", on, True)
check("reconciler: starting twice does not start twice", same, True)
check("reconciler: and it stops cleanly", stopped, True)


tmp.cleanup()
print()
print(f"{len(fails)} failed" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
