"""Memory retrieval is a storage seam, not a runtime concern.

A run has to be able to say which memory records entered its context before this layer
can ever relate memory usage to cost or outcome — and that reporting has to survive the
usual hazards of a per-run write: the same record retrieved twice in one run, and the
same record retrieved by two different runs. Both are exercised here against a real
SQLite file, because `INSERT OR IGNORE` and a composite primary key are exactly the kind
of thing a stub cannot get wrong in the same way the real driver can.

Run it directly, no framework needed:

    .venv/bin/python -m telemetry.store_test
"""
import asyncio
import sys
import tempfile
from pathlib import Path

from telemetry import config, store

fails = []


def check(name, got, want=True):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n       got={got!r} want={want!r}"))
    if not ok:
        fails.append(name)


tmp = tempfile.TemporaryDirectory()
# `config.db_path` is read per call by `store.connect()` rather than bound at import, so
# pointing it at a throwaway file is enough — the real database is never opened.
config.db_path = Path(tmp.name) / "factory.db"
asyncio.run(store.init())


def run(coro):
    return asyncio.run(coro)


# ---------- AC1: memory reads round trip

run(store.write_memory_reads(
    "run-1",
    [("mem_aaaa", "database", "2026-08-20T10:00:00Z"),
     ("mem_bbbb", "auth", "2026-08-20T10:00:01Z")],
))
rows = run(store.memory_reads_for_run("run-1"))
check("both retrieved records come back", [r["memory_id"] for r in rows],
      ["mem_aaaa", "mem_bbbb"])
check("the domain is preserved", [r["domain"] for r in rows], ["database", "auth"])


# ---------- AC2: memory reads are idempotent

run(store.write_memory_reads("run-1", [("mem_aaaa", "database", "2026-08-20T10:00:02Z")]))
rows = run(store.memory_reads_for_run("run-1"))
check("writing the same record again leaves one row",
      len([r for r in rows if r["memory_id"] == "mem_aaaa"]), 1)
check("the first write's timestamp is kept, not overwritten",
      next(r["ts"] for r in rows if r["memory_id"] == "mem_aaaa"), "2026-08-20T10:00:00Z")


# ---------- AC3: memory reads retain run scope

run(store.write_memory_reads("run-2", [("mem_aaaa", "database", "2026-08-20T11:00:00Z")]))
run1_ids = [r["memory_id"] for r in run(store.memory_reads_for_run("run-1"))]
run2_ids = [r["memory_id"] for r in run(store.memory_reads_for_run("run-2"))]
check("run-1 still has its own rows", run1_ids, ["mem_aaaa", "mem_bbbb"])
check("run-2 is attributed its own row for the same memory record", run2_ids, ["mem_aaaa"])


# ---------- an empty batch is a no-op, not an error

run(store.write_memory_reads("run-3", []))
check("a run with no retrievals reads back empty", run(store.memory_reads_for_run("run-3")), [])


if fails:
    print(f"\n{len(fails)} failed: {', '.join(fails)}")
    sys.exit(1)
print("\nall ok")
