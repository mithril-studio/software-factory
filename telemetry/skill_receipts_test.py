"""Skill receipts are a per-run fact that a replay must be able to clear.

The skill loop is gated on receipts, and its history — which skills ran, which found something —
is what a learning run will read to judge whether the loop earns its cost. That only holds if a
skill reported twice in one run is one row (the later report describes the change handed over),
if two runs never share a row, and if `clear_run` takes them with the rest of the run: a
backfill that left old receipts standing would answer "did this run do the loop?" with a yes
from a transcript that no longer says so.

Run it directly, no framework needed:

    .venv/bin/python -m telemetry.skill_receipts_test
"""
import asyncio
import sys
import tempfile
from pathlib import Path

from telemetry import config, store

fails: list[str] = []


def check(name, got, want=True):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n       got={got!r} want={want!r}"))
    if not ok:
        fails.append(name)


def run(coro):
    return asyncio.run(coro)


tmp = tempfile.TemporaryDirectory()
config.db_path = Path(tmp.name) / "factory.db"
run(store.init())
run(store.init())  # a restart

run(store.write_skill_receipt("r1", "slob-detection", "findings", "two dead helpers", "2026-11-02T10:00:00+00:00"))
run(store.write_skill_receipt("r1", "simplification", "changed", "removed them", "2026-11-02T10:01:00+00:00"))
run(store.write_skill_receipt("r1", "slob-detection", "clean", "", "2026-11-02T10:02:00+00:00"))
run(store.write_skill_receipt("r2", "design-check", "clean", None, "2026-11-02T10:03:00+00:00"))

rows = run(store.skill_receipts_for_run("r1"))
check("one row per skill per run", sorted(r["skill"] for r in rows), ["simplification", "slob-detection"])
check("a skill reported again is the later report",
      next(r["verdict"] for r in rows if r["skill"] == "slob-detection"), "clean")
check("another run's receipts are its own", [r["skill"] for r in run(store.skill_receipts_for_run("r2"))],
      ["design-check"])
check("a run that filed none has none", run(store.skill_receipts_for_run("r9")), [])

run(store.clear_run("r1"))
check("clear_run drops the run's skill receipts", run(store.skill_receipts_for_run("r1")), [])
check("and leaves another run's alone", len(run(store.skill_receipts_for_run("r2"))), 1)

print()
print(f"{len(fails)} failed: {', '.join(fails)}" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
