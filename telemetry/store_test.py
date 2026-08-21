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
    [("mem_aaaa", "2026-08-20T10:00:00Z"),
     ("mem_bbbb", "2026-08-20T10:00:01Z")],
))
rows = run(store.memory_reads_for_run("run-1"))
check("both retrieved records come back", [r["memory_id"] for r in rows],
      ["mem_aaaa", "mem_bbbb"])
# A read row carries no domain at all. The receipt names domains as a set over the run, with
# no mapping back to individual records, so any per-row domain would be a guess written down
# as a fact — the defect this table was reshaped to make unrepresentable.
check("a read row cannot claim a domain", "domain" in dict(rows[0]), False)


# ---------- AC2: the domains a run drew from, at run level

run(store.write_memory_receipt("run-1", 9, ["repository", "auth"], "2026-08-20T10:00:03Z"))
receipt = run(store.memory_receipt_for_run("run-1"))
check("the receipt records how big the index was", receipt["indexed"], 9)
check("both domains survive, neither collapsed into the other",
      sorted(receipt["domains"]), ["auth", "repository"])
run(store.write_memory_receipt("run-1", 10, ["repository"], "2026-08-20T10:00:04Z"))
check("a second receipt corrects the first rather than duplicating the run",
      run(store.memory_receipt_for_run("run-1"))["indexed"], 10)
check("a run that filed no receipt reads back empty, not an error",
      run(store.memory_receipt_for_run("run-nothing")), {})

# The migration is what makes this work on a box whose table predates the change; it must be
# safe to apply to a database that has already had it applied.
run(store.init())
run(store.init())
check("init is idempotent across the column drop", run(store.memory_receipt_for_run("run-1"))["indexed"], 10)


# ---------- AC2: memory reads are idempotent

run(store.write_memory_reads("run-1", [("mem_aaaa", "2026-08-20T10:00:02Z")]))
rows = run(store.memory_reads_for_run("run-1"))
check("writing the same record again leaves one row",
      len([r for r in rows if r["memory_id"] == "mem_aaaa"]), 1)
check("the first write's timestamp is kept, not overwritten",
      next(r["ts"] for r in rows if r["memory_id"] == "mem_aaaa"), "2026-08-20T10:00:00Z")


# ---------- AC3: memory reads retain run scope

run(store.write_memory_reads("run-2", [("mem_aaaa", "2026-08-20T11:00:00Z")]))
run1_ids = [r["memory_id"] for r in run(store.memory_reads_for_run("run-1"))]
run2_ids = [r["memory_id"] for r in run(store.memory_reads_for_run("run-2"))]
check("run-1 still has its own rows", run1_ids, ["mem_aaaa", "mem_bbbb"])
check("run-2 is attributed its own row for the same memory record", run2_ids, ["mem_aaaa"])


# ---------- an empty batch is a no-op, not an error

run(store.write_memory_reads("run-3", []))
check("a run with no retrievals reads back empty", run(store.memory_reads_for_run("run-3")), [])


# ---------- usage includes memory reads

run(store.write_memory_reads("run-4", [("mem_cccc", "2026-08-20T12:00:00Z")]))
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
run(store.write_memory_reads("run-repo-a-1", [("mem_dddd", "2026-08-20T12:00:00Z")]))
run(store.write_memory_reads("run-repo-a-2", [("mem_dddd", "2026-08-20T12:00:01Z"),
                                              ("mem_eeee", "2026-08-20T12:00:02Z")]))
run(store.write_memory_reads("run-repo-b-1", [("mem_ffff", "2026-08-20T12:00:03Z")]))
run(store.write_memory_receipt("run-repo-a-1", 4, ["database"], "2026-08-20T12:00:00Z"))
run(store.write_memory_receipt("run-repo-a-2", 4, ["database", "auth"], "2026-08-20T12:00:02Z"))
run(store.write_memory_receipt("run-repo-b-1", 4, ["database"], "2026-08-20T12:00:03Z"))

by_repo = {row["repo"]: row for row in run(store.memory_metrics_by_repo())}
check("both repositories with memory reads are reported", sorted(by_repo), ["org/a", "org/b"])
check("org/a counts both its runs with memory", by_repo["org/a"]["runs_with_memory"], 2)
check("org/a's distinct records are not double-counted across its two runs",
      by_repo["org/a"]["distinct_records"], 2)
check("org/b counts its own run only", by_repo["org/b"]["runs_with_memory"], 1)
check("org/b's distinct records are not attributed to org/a",
      by_repo["org/b"]["distinct_records"], 1)
# The old per-row column would have reported org/a as `database` only, because `database`
# was the first domain of the first receipt and every row inherited it.
check("org/a reports every domain its runs drew from, not just the first",
      by_repo["org/a"]["domains"], ["auth", "database"])
check("org/b's domains are its own", by_repo["org/b"]["domains"], ["database"])
check("every repo with memory reads has a positive average cost",
      all(row["avg_derived_cost_usd"] > 0 for row in by_repo.values()))


# ---------- a run that primed and opened nothing is still a run that primed
#
# Reading only `memory_reads` made it invisible, and with it every repo whose runs all look
# like that — which is precisely the repo whose memory is not earning its keep, the one an
# operator most needs to see. `runs_with_memory` still means "retrieved a record"; the count
# of runs that got as far as priming is its own number.

async def seed_primed_only():
    async with aiosqlite.connect(config.db_path) as conn:
        await conn.execute(
            "INSERT INTO runs (id, repo, issue_number, issue_title, branch, status, attempt, "
            "agent, log_path, created_at) VALUES ('run-repo-c-1', 'org/c', 1, 't', 'b', 's', 1, "
            "'a', 'l', '2026-08-20T00:00:00Z')"
        )
        await conn.execute(
            "INSERT INTO llm_calls (run_id, turn, ts, model, input_tokens, output_tokens) "
            "VALUES ('run-repo-c-1', 1, '2026-08-20T00:00:00Z', 'claude-sonnet-5', 1000, 1000)"
        )
        await conn.commit()


run(seed_primed_only())
run(store.write_memory_receipt("run-repo-c-1", 14, [], "2026-08-20T13:00:00Z"))

by_repo = {row["repo"]: row for row in run(store.memory_metrics_by_repo())}
check("a repo whose only run primed but opened nothing is still reported",
      "org/c" in by_repo, True)
check("that run counts as primed", by_repo["org/c"]["runs_primed"], 1)
check("but not as a run that used memory", by_repo["org/c"]["runs_with_memory"], 0)
check("and it retrieved no records", by_repo["org/c"]["distinct_records"], 0)
check("a repo whose runs both primed and opened counts both ways",
      (by_repo["org/a"]["runs_primed"], by_repo["org/a"]["runs_with_memory"]), (2, 2))


# ---------- clear_run means the run
#
# A backfill replays a run's transcript over the top of whatever is already stored. Leaving
# the memory rows behind was survivable only while the replay produced an identical receipt;
# a replay of a corrected transcript, or one that finds no receipt at all, would otherwise
# leave the old rows standing as the run's answer forever.

run(store.write_memory_reads("run-clear", [("mem_9999", "2026-08-20T14:00:00Z")]))
run(store.write_memory_receipt("run-clear", 7, ["repository"], "2026-08-20T14:00:00Z"))
check("before clearing, the run has its memory rows",
      (len(run(store.memory_reads_for_run("run-clear"))),
       bool(run(store.memory_receipt_for_run("run-clear")))), (1, True))
run(store.clear_run("run-clear"))
check("clear_run drops the run's memory reads",
      run(store.memory_reads_for_run("run-clear")), [])
check("clear_run drops the run's receipt",
      run(store.memory_receipt_for_run("run-clear")), {})
check("clear_run left another run's memory rows alone",
      len(run(store.memory_reads_for_run("run-repo-a-2"))), 2)


if fails:
    print(f"\n{len(fails)} failed: {', '.join(fails)}")
    sys.exit(1)
print("\nall ok")
