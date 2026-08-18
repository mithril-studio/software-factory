"""How a run gets its VM: created from a snapshot, or forked from a machine.

Goldens are moving from long-lived machines to snapshots, so `_provision` has to serve both
kinds of source at once — the fork path is the rollback for the whole migration, and it has
to keep working while the snapshot path is proven.

Every check below is really the same question from a different side: does the source decide
the API, and does each API get exactly the arguments it accepts? A snapshot restore is not a
fork with a different word for it. It rejects `env`, `image`, `cmd`, `restart_policy`,
`shared` and `networks` outright, and it needs the same two timers a fork needs — without
them a VM either freezes mid-build or leaks one of the account's 20 slots forever.

Nothing here talks to boxd. The stub records what was asked of it and enforces the real
SDK's rule about rejected keywords, so a wrong call fails here rather than in a live run.

Run it directly, no framework needed:

    .venv/bin/python -m control.provision_test
"""
import asyncio
import sys

from control.config import settings
from control.runner import _provision

fails = []


def check(name, got, want=True):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n       got={got!r} want={want!r}"))
    if not ok:
        fails.append(name)


class Thing:
    """A machine, a snapshot, or the machine handed back by create/fork — all id + name."""

    def __init__(self, id, name):
        self.id, self.name = id, name


# The keywords `create(from_snapshot=...)` refuses, straight from the SDK: restoring a
# capture replays a machine as it was, it does not configure a new one.
REJECTED = ("image", "env", "cmd", "restart_policy", "shared", "networks")


class StubMachines:
    def __init__(self, machines, calls):
        self._machines, self._calls = machines, calls

    async def list(self):
        self._calls.append(("machines.list", {}))
        return self._machines

    async def create(self, name=None, **kw):
        self._calls.append(("create", {"name": name, **kw}))
        # Same rule as the real SDK, so passing one of these fails here, not in a live run.
        bad = [k for k in REJECTED if kw.get(k)]
        if bad:
            raise ValueError(f"create(from_snapshot=...) does not accept {', '.join(bad)}")
        return Thing("vm-new", name)

    async def fork(self, source, name=None, **kw):
        self._calls.append(("fork", {"source": source, "name": name, **kw}))
        return Thing("vm-forked", name)


class StubSnapshots:
    def __init__(self, snapshots, calls):
        self._snapshots, self._calls = snapshots, calls

    async def list(self):
        self._calls.append(("snapshots.list", {}))
        return self._snapshots


class StubBoxd:
    """Records every call, so a test can assert which API a source reached."""

    def __init__(self, machines=(), snapshots=()):
        self.calls = []
        self.machines = StubMachines(list(machines), self.calls)
        self.snapshots = StubSnapshots(list(snapshots), self.calls)

    def named(self, call):
        """The kwargs of the one call named `call`, or None if it never happened."""
        found = [kw for name, kw in self.calls if name == call]
        return found[0] if found else None


GOLDEN = "golden-claude"
VM = "run-abc12345"


def provision(boxd, source=GOLDEN, vm_name=VM):
    return asyncio.run(_provision(boxd, source, vm_name))


# ---------- AC1: a source naming a snapshot is created from, not forked

snap = StubBoxd(
    machines=[Thing("m-1", GOLDEN)],  # a same-named machine must not win the tie
    snapshots=[Thing("s-1", GOLDEN)],
)
machine = provision(snap)

check("from snapshot: the snapshot path creates the VM", bool(snap.named("create")))
check("from snapshot: and does not fork", snap.named("fork"), None)
check("from snapshot: restores the source it was given",
      (snap.named("create") or {}).get("from_snapshot"), GOLDEN)
check("from snapshot: names the VM as asked", (snap.named("create") or {}).get("name"), VM)
check("from snapshot: returns the created machine", machine.id, "vm-new")

# A snapshot known only by id is still a snapshot.
by_id = StubBoxd(snapshots=[Thing("s-2", GOLDEN)])
provision(by_id, source="s-2")
check("from snapshot: an id names a snapshot as well as a name does",
      (by_id.named("create") or {}).get("from_snapshot"), "s-2")


# ---------- AC2: a source naming a machine still resolves its id and forks

fleet = StubBoxd(machines=[Thing("m-9", GOLDEN), Thing("m-2", "other")], snapshots=[])
forked = provision(fleet)

check("from machine: the fork path forks", bool(fleet.named("fork")))
check("from machine: and never creates", fleet.named("create"), None)
check("from machine: forks the resolved id, not the name",
      (fleet.named("fork") or {}).get("source"), "m-9")
check("from machine: names the VM as asked", (fleet.named("fork") or {}).get("name"), VM)
check("from machine: returns the forked machine", forked.id, "vm-forked")

# An unrelated snapshot in the account must not divert a machine source.
mixed = StubBoxd(machines=[Thing("m-3", GOLDEN)], snapshots=[Thing("s-3", "golden-codex")])
provision(mixed)
check("from machine: somebody else's snapshot does not divert the fork",
      (mixed.named("fork") or {}).get("source"), "m-3")

# Neither a snapshot nor a machine: say so, rather than provisioning something arbitrary.
missing = StubBoxd()
try:
    provision(missing, source="nowhere")
    check("from machine: a source that is neither is an error", False)
except RuntimeError as e:
    check("from machine: a source that is neither is an error", "nowhere" in str(e))


# ---------- AC3: both paths disable auto-suspend and pass the auto-destroy timeout

for label, call, boxd in (
    ("snapshot", "create", snap),
    ("machine", "fork", fleet),
):
    kw = boxd.named(call) or {}
    check(f"timers: the {label} path disables auto-suspend", kw.get("auto_suspend_timeout"), 0)
    check(f"timers: the {label} path passes the auto-destroy timeout",
          kw.get("auto_destroy_timeout"), settings.auto_destroy)


# ---------- AC4: the snapshot path passes nothing that API rejects

created = snap.named("create") or {}
check("rejected kwargs: none of the keywords create(from_snapshot=...) refuses",
      sorted(k for k in REJECTED if k in created), [])
check("rejected kwargs: only the four arguments a restore accepts",
      sorted(created), ["auto_destroy_timeout", "auto_suspend_timeout", "from_snapshot", "name"])

# The stub raises exactly as the SDK does, so the guard above is a real guard.
guard = StubBoxd(snapshots=[Thing("s-4", GOLDEN)])
try:
    asyncio.run(guard.machines.create(name=VM, from_snapshot=GOLDEN, env={"A": "1"}))
    check("rejected kwargs: the stub enforces the SDK's rule", False)
except ValueError as e:
    check("rejected kwargs: the stub enforces the SDK's rule", "env" in str(e))


print()
if fails:
    print(f"{len(fails)} failed: " + ", ".join(fails))
    sys.exit(1)
print("all passed")
