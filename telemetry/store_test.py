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

import aiosqlite

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


# ---------- usage includes memory reads

run(store.write_memory_reads("run-4", [("mem_cccc", "database", "2026-08-20T12:00:00Z")]))
usage = run(store.usage_for_run("run-4"))
check("usage for a run with no llm calls still reports its memory reads",
      [r["memory_id"] for r in usage["memory"]], ["mem_cccc"])
check("usage for a run with no memory reads reports an empty list, not an error",
      run(store.usage_for_run("run-nonexistent"))["memory"], [])


# ---------- memory metrics group by repository

async def seed_repo_metrics():
    """`memory_metrics_by_repo` reads `runs.repo`, control's table — this store never
    creates it (SCHEMA only owns telemetry's own tables), so the test creates a minimal
    stand-in the same way `control/db.py` would, on the same throwaway file."""
    async with aiosqlite.connect(config.db_path) as conn:
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS runs (
                   id TEXT PRIMARY KEY, repo TEXT NOT NULL, issue_number INTEGER,
                   issue_title TEXT, branch TEXT, status TEXT, attempt INTEGER,
                   agent TEXT, log_path TEXT, created_at TEXT
               )"""
        )
        for run_id, repo in (("run-repo-a-1", "org/a"), ("run-repo-a-2", "org/a"),
                              ("run-repo-b-1", "org/b")):
            await conn.execute(
                "INSERT INTO runs (id, repo, issue_number, issue_title, branch, status, "
                "attempt, agent, log_path, created_at) VALUES (?, ?, 1, 't', 'b', 's', 1, 'a', "
                "'l', '2026-08-20T00:00:00Z')",
                (run_id, repo),
            )
        await conn.executemany(
            "INSERT INTO llm_calls (run_id, turn, ts, model, input_tokens, output_tokens) "
            "VALUES (?, 1, '2026-08-20T00:00:00Z', 'claude-sonnet-5', 1000, 1000)",
            [("run-repo-a-1",), ("run-repo-a-2",), ("run-repo-b-1",)],
        )
        await conn.commit()


run(seed_repo_metrics())
run(store.write_memory_reads("run-repo-a-1", [("mem_dddd", "database", "2026-08-20T12:00:00Z")]))
run(store.write_memory_reads("run-repo-a-2", [("mem_dddd", "database", "2026-08-20T12:00:01Z"),
                                               ("mem_eeee", "auth", "2026-08-20T12:00:02Z")]))
run(store.write_memory_reads("run-repo-b-1", [("mem_ffff", "database", "2026-08-20T12:00:03Z")]))

by_repo = {row["repo"]: row for row in run(store.memory_metrics_by_repo())}
check("both repositories with memory reads are reported", sorted(by_repo), ["org/a", "org/b"])
check("org/a counts both its runs with memory", by_repo["org/a"]["runs_with_memory"], 2)
check("org/a's distinct records are not double-counted across its two runs",
      by_repo["org/a"]["distinct_records"], 2)
check("org/b counts its own run only", by_repo["org/b"]["runs_with_memory"], 1)
check("org/b's distinct records are not attributed to org/a",
      by_repo["org/b"]["distinct_records"], 1)
check("every repo with memory reads has a positive average cost",
      all(row["avg_derived_cost_usd"] > 0 for row in by_repo.values()))


if fails:
    print(f"\n{len(fails)} failed: {', '.join(fails)}")
    sys.exit(1)
print("\nall ok")
