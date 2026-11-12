"""Skill usage is already in the trace layer; the only question is whether we ask.

A self-improving loop that can only add context makes cache reads — 73.5% of spend —
monotonically worse. Deletion is the other half, and it has to rest on evidence rather
than on an agent's opinion of its own past advice. `skill_loads_by_repo` is that evidence:
a skill load is a tool call, tool calls are already rows, so "which skills does this repo
actually use" needs no new receipt, no new table, and no new promise from the agent.

What matters here is that the join holds on the two things it silently depends on — the
runtime's name for the tool, and `skill` surviving `_hint` into `detail` — because if
either drifts the query does not break, it quietly returns nothing, and a loop reading it
would evict every skill in the repo as unused.

Run it directly, no framework needed:

    .venv/bin/python -m telemetry.skill_loads_test
"""
import asyncio
import sys
import tempfile
from pathlib import Path

import aiosqlite

from telemetry import config, normalize, store

fails = []


def check(name, got, want=True):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n       got={got!r} want={want!r}"))
    if not ok:
        fails.append(name)


tmp = tempfile.TemporaryDirectory()
config.db_path = Path(tmp.name) / "factory.db"
asyncio.run(store.init())


def run(coro):
    return asyncio.run(coro)


async def seed_runs(rows):
    """`runs` belongs to `control`, which this layer reads and never writes. Created here
    by hand for the same reason `store_test` uses a real SQLite file: the join is the thing
    under test, and a stub cannot get a join wrong the way the driver can."""
    async with aiosqlite.connect(config.db_path) as conn:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS runs ("
            "id TEXT PRIMARY KEY, repo TEXT, issue_number INTEGER, status TEXT,"
            " kind TEXT, pr_url TEXT, base_sha TEXT, created_at TEXT)"
        )
        await conn.executemany(
            "INSERT OR REPLACE INTO runs (id, repo, kind, base_sha) VALUES (?, ?, ?, ?)", rows
        )
        await conn.commit()


run(seed_runs([
    ("run-a", "acme/web", "build", "sha1"),
    ("run-b", "acme/web", "build", "sha1"),
    ("run-c", "acme/api", "build", "sha2"),
]))


def tool_call(call_id, run_id, tool, detail, ts):
    return normalize.ToolCall(
        id=call_id, run_id=run_id, turn=1, ts=ts, tool=tool,
        ok=True, duration_ms=10, error=None, detail=detail,
    )


run(store.write([
    tool_call("t1", "run-a", normalize.SKILL_TOOL, "verify-before-pr", "2026-08-20T10:00:00Z"),
    tool_call("t2", "run-a", normalize.SKILL_TOOL, "verify-before-pr", "2026-08-20T11:00:00Z"),
    tool_call("t3", "run-b", normalize.SKILL_TOOL, "verify-before-pr", "2026-08-21T10:00:00Z"),
    tool_call("t4", "run-b", normalize.SKILL_TOOL, "migration-safety", "2026-08-21T10:05:00Z"),
    tool_call("t5", "run-c", normalize.SKILL_TOOL, "verify-before-pr", "2026-08-21T12:00:00Z"),
    # Not a skill load. Present so the filter has something to exclude that looks alike.
    tool_call("t6", "run-a", "Bash", "npm test", "2026-08-20T10:30:00Z"),
]))


# ---------- AC1: loads roll up per repo and skill

rows = run(store.skill_loads_by_repo())
web = [r for r in rows if r["repo"] == "acme/web"]
check("both of the repo's skills come back", sorted(r["skill"] for r in web),
      ["migration-safety", "verify-before-pr"])

heavy = next(r for r in web if r["skill"] == "verify-before-pr")
check("loads count invocations, not runs", heavy["loads"], 3)
check("runs count distinct runs", heavy["runs"], 2)
check("last_loaded is the newest timestamp", heavy["last_loaded"], "2026-08-21T10:00:00Z")

# A Bash call must not be mistaken for a skill load. If HINT_KEYS ever reordered so that a
# non-skill tool's input filled `detail` from the `skill` key, every tool in the run would
# arrive here as a phantom skill and eviction would have nothing left to delete.
check("non-skill tools are excluded", any(r["skill"] == "npm test" for r in rows), False)


# ---------- AC2: repos do not bleed into each other

api = [r for r in rows if r["repo"] == "acme/api"]
check("the other repo is scoped to its own run", [(r["skill"], r["loads"]) for r in api],
      [("verify-before-pr", 1)])


# ---------- AC3: the window filter, which is what makes "unused lately" answerable

recent = run(store.skill_loads_by_repo(since="2026-08-21T00:00:00Z"))
recent_web = {r["skill"]: r["loads"] for r in recent if r["repo"] == "acme/web"}
check("the window drops the older loads", recent_web,
      {"verify-before-pr": 1, "migration-safety": 1})

# The eviction signal itself: a skill present in the repo and absent from this result across
# the window is unused. Asserted as the shape a caller relies on rather than left implied —
# a skill that stopped being loaded must *disappear*, not come back with loads=0.
future = run(store.skill_loads_by_repo(since="2026-09-01T00:00:00Z"))
check("a skill unused in the window is absent, not zero", future, [])


# ---------- AC4: the adapter owns the runtime's name for the tool

# `docs/architecture.md` §3.2: no table names a runtime. The query imports the constant so
# that a second runtime calling this something else is one line in the adapter, not a grep.
check("the tool name comes from the adapter", normalize.SKILL_TOOL, "Skill")
check("skill survives _hint into detail", normalize._hint({"skill": "abc", "args": "x"}), "abc")


print()
if fails:
    print(f"{len(fails)} failed: {', '.join(fails)}")
    sys.exit(1)
print("all passed")
