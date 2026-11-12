"""What a run costs: every current model has a price, and a missing one is visible.

The spend ceiling and every cost view read a join against `model_prices`. A model with no row
there joins to nothing and costs $0 — silently, which is how the newest models ended up
switching the old ceilings off. So the current family is pinned here, an unknown model is
counted rather than hidden, a transcript replay leaves the receipts only the live stream can
write, and the cost-per-issue view counts builds and nothing else.

Run it directly, no framework needed:

    .venv/bin/python -m telemetry.pricing_test
"""
import asyncio
import sys
import tempfile
from pathlib import Path

import aiosqlite

from telemetry import config, store
from telemetry.normalize import LlmCall

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


def call(run_id, model, tokens_in=1_000_000, tokens_out=0):
    return LlmCall(run_id=run_id, turn=1, ts="2026-11-12T10:00:00Z", model=model,
                   input_tokens=tokens_in, output_tokens=tokens_out, cache_read_tokens=0,
                   cache_write_5m_tokens=0, cache_write_1h_tokens=0, parent_call_id=None)


def cost(run_id):
    return run(store.usage_for_run(run_id))["totals"]


# ---------- the current family is priced
for model, per_million_in in (
    ("claude-opus-5-5", 4.00),
    ("claude-sonnet-5-5", 2.00),
    ("claude-haiku-5-5", 0.10),
    ("claude-fable-5-2", 8.00),
    ("claude-fable-5-1", 10.00),
):
    run(store.write([call(f"r-{model}", model)]))
    t = cost(f"r-{model}")
    check(f"price: a million input tokens on {model} costs ${per_million_in:.2f}",
          round(t["derived_cost_usd"], 4), per_million_in)
    check(f"price: and no {model} call is unpriced", t["unpriced_calls"], 0)

run(store.write([call("r-out", "claude-opus-5-5", tokens_in=0, tokens_out=1_000_000)]))
check("price: output is priced on its own rate", round(cost("r-out")["derived_cost_usd"], 4), 20.00)

# ---------- an unknown model is visible, not free
run(store.write([call("r-new", "claude-unknown-9"), call("r-new", "claude-opus-5-5")]))
t = cost("r-new")
check("unpriced: the call is counted", t["unpriced_calls"], 1)
check("unpriced: the priced call still costs what it costs", round(t["derived_cost_usd"], 4), 4.00)
by_model = {m["model"]: m for m in run(store.usage_for_run("r-new"))["by_model"]}
check("unpriced: the per-model view says which one", by_model["claude-unknown-9"]["priced"], 0)

# ---------- a replay keeps what it cannot rebuild
run(store.write([call("r-replay", "claude-opus-5-5")]))
run(store.write_memory_receipt("r-replay", 12, ["repository"]))
run(store.replace_calls("r-replay", [call("r-replay", "claude-sonnet-5-5")]))
t = cost("r-replay")
check("replay: the calls are replaced, not added", (t["calls"], round(t["derived_cost_usd"], 4)), (1, 2.00))
check("replay: the memory receipt only the live stream writes survives",
      run(store.memory_receipt_for_run("r-replay")).get("indexed"), 12)


# ---------- cost per issue counts builds only
async def seed_runs():
    async with aiosqlite.connect(config.db_path) as conn:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, repo TEXT, issue_number INTEGER,"
            " kind TEXT, pr_url TEXT, status TEXT, created_at TEXT)"
        )
        await conn.executemany(
            "INSERT INTO runs (id, repo, issue_number, kind, pr_url) VALUES (?, ?, ?, ?, ?)",
            [("b1", "acme/api", 7, "build", "https://x/pull/1"),
             ("p1", "acme/api", 0, "plan", None),
             ("l1", "acme/api", 0, "learn", None)],
        )
        await conn.commit()


run(seed_runs())
run(store.write([call("b1", "claude-opus-5-5"), call("p1", "claude-opus-5-5"), call("l1", "claude-opus-5-5")]))
row = run(store.unit_economics())[0]
check("economics: plan and learn runs are not issues", row["issues"], 1)
check("economics: nor are they waste", round(row["wasted"], 4), 0.0)
check("economics: the build's spend is the spend", round(row["spend"], 4), 4.00)
check("economics: and it shipped", row["shipped"], 1)

print()
print(f"{len(fails)} failed" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
