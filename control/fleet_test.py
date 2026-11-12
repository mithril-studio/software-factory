"""Not leaking machines, and not losing runs: the reap, the sweep, and restarts.

Each section is a way a VM slot or an issue used to go missing quietly:

1. **The reap** must be idempotent and never raise out of a `finally`.
2. **The headroom check** refuses a full fleet, but sweeps for orphans first.
3. **The sweep** touches only machines carrying this control plane's prefix — another
   control plane on the same boxd account must be invisible to it — and one machine that
   will not die must not stop the rest. A run with no task behind it is failed, but only once
   it is older than the moment between its row being written and its task registering.
4. **A restart** used to leave issues labelled `agent:running` forever. Now every unfinished
   run is closed on boot and its issue goes back on the queue.

Nothing here talks to boxd or GitHub. The database is real, against a throwaway file.

Run it directly, no framework needed:

    .venv/bin/python -m control.fleet_test
"""
import asyncio
import sys
import tempfile
from pathlib import Path

from control import db, github, runner
from control.config import settings

fails = []


def check(name, got, want=True):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n       got={got!r} want={want!r}"))
    if not ok:
        fails.append(name)


tmp = tempfile.TemporaryDirectory()
object.__setattr__(settings, "db_path", Path(tmp.name) / "factory.db")
object.__setattr__(settings, "vm_prefix", "job-")
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


def old(seconds=600):
    import datetime as dt
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds)).isoformat(timespec="seconds")


# ---------- names
check("name: a run's VM carries the prefix", runner.vm_name("abcdef0123456789"), "job-abcdef01")
check("name: ours", runner.is_ours("job-abcdef01"))
check("name: the old pipeline's run- machines are not ours", runner.is_ours("run-abcdef01"), False)
check("name: nor is a golden or the control plane",
      (runner.is_ours("golden-copy"), runner.is_ours("mithril-factory")), (False, False))

# ---------- reap
boxd, log = FakeBoxd(["job-a"]), Log()
run(runner._reap(boxd, Thing("id-job-a", "job-a"), log))
check("reap: destroys the machine and says so", (boxd.names, log.said("destroyed job-a")), ([], True))
log = Log()
run(runner._reap(boxd, None, log))
check("reap: nothing to reap is not an error", log.lines, [])
boxd, log = FakeBoxd(["job-keep"]), Log()
run(runner._reap(boxd, Thing("id-job-keep", "job-keep"), log, keep=True))
check("reap: keep leaves the machine alone", boxd.names, ["job-keep"])
boxd, log = FakeBoxd(["job-stuck"], undead=["job-stuck"]), Log()
run(runner._reap(boxd, Thing("id-job-stuck", "job-stuck"), log))
check("reap: a machine that refuses to die is logged, not raised", log.said("reconcile will sweep it"))

# ---------- headroom
object.__setattr__(settings, "max_machines", 4)
boxd, log = FakeBoxd(["golden-copy", "mithril-factory"]), Log()
runner.client = lambda: boxd
run(runner._headroom(boxd, log))
check("headroom: room to spare is silent", log.lines, [])

boxd, log = FakeBoxd(["golden-copy", "mithril-factory", "job-orphan1", "job-orphan2"]), Log()
runner.client = lambda: boxd
run(runner._headroom(boxd, log))
check("headroom: at the cap it sweeps our orphans and proceeds",
      sorted(boxd.names), ["golden-copy", "mithril-factory"])

boxd, log = FakeBoxd(["golden-copy", "mithril-factory", "run-other-plane", "scratch"]), Log()
runner.client = lambda: boxd
try:
    run(runner._headroom(boxd, log))
    check("headroom: a full fleet with nothing of ours to sweep refuses", False)
except RuntimeError as exc:
    check("headroom: a full fleet with nothing of ours to sweep refuses", "4/4" in str(exc))
check("headroom: and touches nothing that is not ours", len(boxd.names), 4)
object.__setattr__(settings, "max_machines", 0)
boxd, log = FakeBoxd(["a"] * 30), Log()
run(runner._headroom(boxd, log))
check("headroom: 0 switches the check off", log.lines, [])

# ---------- reconcile
run(db.claim_run(id="live", repo="acme/api", issue_number=1, status="running",
                 vm_name="job-live", created_at=old()))
runner._tasks["live"] = "pretend-task"
boxd = FakeBoxd(["golden-copy", "mithril-factory", "job-live", "job-orphan", "job-stubborn",
                 "run-other-plane"], undead=["job-stubborn"])
runner.client = lambda: boxd
found = run(runner.reconcile())
check("reconcile: every orphan of ours is attempted", sorted(boxd.deletes), ["job-orphan", "job-stubborn"])
check("reconcile: the one that went is reported", found["destroyed"], ["job-orphan"])
check("reconcile: the one that would not is named", found["stuck"], ["job-stubborn"])
check("reconcile: a VM with a live run behind it is left alone", "job-live" in boxd.names)
check("reconcile: another control plane's machine is invisible", "run-other-plane" in boxd.names)
check("reconcile: a run with a task is untouched", run(db.get_run("live"))["status"], "running")

# A VM whose name is already claimed by a run that is still being provisioned is not an orphan.
run(db.claim_run(id="young", repo="acme/web", issue_number=2, status="queued",
                 vm_name="job-young", created_at=db.utcnow()))
boxd = FakeBoxd(["job-young"])
runner.client = lambda: boxd
found = run(runner.reconcile())
check("reconcile: a VM being created for a claimed run is never swept", boxd.names, ["job-young"])
check("reconcile: a just-claimed run without its task yet is not 'lost'", found["lost"], [])

run(db.claim_run(id="ghost", repo="acme/ops", issue_number=3, status="running",
                 vm_name="job-ghost", created_at=old()))
found = run(runner.reconcile())
check("reconcile: an older run with no task behind it is failed", found["lost"], ["ghost"])
check("reconcile: with a reason somebody can read",
      run(db.get_run("ghost"))["error"], "lost: no task behind this run")
runner._tasks.pop("live")

# ---------- recover on boot
for rid, repo, number, kind in (("r-build", "acme/a", 5, "build"), ("r-review", "acme/b", 6, "review")):
    run(db.claim_run(id=rid, repo=repo, issue_number=number, status="running", kind=kind,
                     created_at=db.utcnow()))
# A row a crash left unfinished months ago: closing it must not start building its issue today.
run(db.claim_run(id="r-ancient", repo="acme/c", issue_number=7, status="forking", kind="build",
                 created_at="2026-08-20T10:00:00+00:00"))

writes = []


async def remove_label(repo, number, label):
    writes.append(("remove", repo, number, label))


async def add_labels(repo, number, labels):
    writes.append(("add", repo, number, tuple(labels)))


async def add_comment(repo, number, body):
    writes.append(("comment", repo, number))


real = (github.remove_label, github.add_labels, github.add_comment)
github.remove_label, github.add_labels, github.add_comment = remove_label, add_labels, add_comment
try:
    recovered = run(runner.recover())
finally:
    github.remove_label, github.add_labels, github.add_comment = real

check("recover: every unfinished run is closed",
      {"r-build", "r-review", "r-ancient", "young"} <= set(recovered))
check("recover: as failed, saying why", run(db.get_run("r-build"))["error"],
      "interrupted: the control plane restarted mid-run")
check("recover: an interrupted build goes back on the queue",
      ("add", "acme/a", 5, ("agent:queued",)) in writes)
check("recover: and stops claiming to be running",
      ("remove", "acme/a", 5, "agent:running") in writes)
check("recover: the issue is told why", ("comment", "acme/a", 5) in writes)
check("recover: a run of an old kind is closed but nothing is re-queued for it",
      [w for w in writes if w[1] == "acme/b"], [])
check("recover: an ancient leftover is closed too", run(db.get_run("r-ancient"))["status"], "failed")
check("recover: saying it was lost, not interrupted",
      run(db.get_run("r-ancient"))["error"], "lost: left unfinished by an earlier crash")
check("recover: its issue is NOT re-queued", ("add", "acme/c", 7, ("agent:queued",)) in writes, False)
check("recover: but stops claiming to be running, and is marked failed",
      [w for w in writes if w[1] == "acme/c"],
      [("remove", "acme/c", 7, "agent:running"), ("add", "acme/c", 7, ("agent:failed",))])
check("recover: without a comment on an old issue", ("comment", "acme/c", 7) in writes, False)
check("recover: nothing is left active", run(db.active_runs()), [])
check("recover: a second boot finds nothing to do", run(runner.recover()), [])

# ---------- FACTORY_KEEP_FAILED keeps the VM from the sweep too
run(db.claim_run(id="kept", repo="acme/k", issue_number=1, status="running", vm_name="job-kept",
                 created_at=db.utcnow()))
run(db.finish_run("kept", "failed", error="x"))
boxd = FakeBoxd(["job-kept"])
runner.client = lambda: boxd
object.__setattr__(settings, "keep_failed", True)
run(runner.reconcile())
check("keep: a failed run's VM survives the sweep while keep_failed is on", boxd.names, ["job-kept"])
object.__setattr__(settings, "keep_failed", False)
run(runner.reconcile())
check("keep: and is swept once it is off", boxd.names, [])

# ---------- the loop is switched by a setting


async def loop_runs():
    object.__setattr__(settings, "reconcile_interval", 0)
    runner.start_reconciler()
    off = runner._reconciler is None
    object.__setattr__(settings, "reconcile_interval", 300)
    runner.start_reconciler()
    on = runner._reconciler is not None
    await runner.stop_reconciler()
    return off, on, runner._reconciler is None


check("reconciler: off at 0, on otherwise, stops cleanly", run(loop_runs()), (True, True, True))

tmp.cleanup()
print()
print(f"{len(fails)} failed" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
