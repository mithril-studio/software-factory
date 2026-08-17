"""Storage and queries for the trace layer.

Same database as `control`, its own tables and its own connection helper. The two layers
share the database and the `run_id`, and that is the entire coupling — `telemetry` never
imports `control.db`, and `control` never reads a telemetry table. Splitting this into
its own service later is then a matter of pointing the connection somewhere else.

SQLite for now, plain SQL throughout, for the same reason `control/db.py` gives: moving
to Postgres should be a driver swap and a handful of placeholders, not a rewrite.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import AsyncIterator, Sequence
from typing import Any

import aiosqlite

from . import config
from .normalize import LlmCall, ToolCall

SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_calls (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                TEXT NOT NULL,
    turn                  INTEGER NOT NULL,
    ts                    TEXT,
    model                 TEXT,
    input_tokens          INTEGER NOT NULL DEFAULT 0,
    output_tokens         INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_write_5m_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_1h_tokens INTEGER NOT NULL DEFAULT 0,
    parent_call_id        TEXT
);
CREATE INDEX IF NOT EXISTS llm_calls_run_idx ON llm_calls (run_id);

CREATE TABLE IF NOT EXISTS tool_calls (
    id             TEXT PRIMARY KEY,
    run_id         TEXT NOT NULL,
    turn           INTEGER NOT NULL,
    ts             TEXT,
    tool           TEXT NOT NULL,
    ok             INTEGER NOT NULL,
    duration_ms    INTEGER,
    error          TEXT,
    detail         TEXT,
    parent_call_id TEXT
);
CREATE INDEX IF NOT EXISTS tool_calls_run_idx  ON tool_calls (run_id);
CREATE INDEX IF NOT EXISTS tool_calls_tool_idx ON tool_calls (tool);

CREATE TABLE IF NOT EXISTS model_prices (
    model                   TEXT NOT NULL,
    valid_from              TEXT NOT NULL,
    input_per_mtok          REAL NOT NULL,
    output_per_mtok         REAL NOT NULL,
    cache_read_per_mtok     REAL NOT NULL,
    cache_write_5m_per_mtok REAL NOT NULL,
    cache_write_1h_per_mtok REAL NOT NULL,
    PRIMARY KEY (model, valid_from)
);
"""

# Published list prices per million tokens, as of 2026-08-17. Cache rates are the
# documented multiples of the input rate: reads 0.1x, 5-minute writes 1.25x, 1-hour
# writes 2x. This table is ours to edit — it is the reason cost is a join rather than a
# column, and the reason a run on a flat-rate subscription can still be priced honestly
# against what the same work would have cost metered.
#
# `valid_from` is what makes a price change non-destructive: add a row, never edit one,
# and historical runs keep costing what they cost. Claude Sonnet 5's introductory rate
# is the worked example — it reverts on 2026-09-01, and both rows live here today.
SEED_PRICES: tuple[tuple, ...] = (
    # model, valid_from, in, out, cache_read, cache_write_5m, cache_write_1h
    ("claude-opus-5", "2026-01-01", 5.00, 25.00, 0.50, 6.25, 10.00),
    ("claude-opus-4-8", "2026-01-01", 5.00, 25.00, 0.50, 6.25, 10.00),
    ("claude-opus-4-7", "2026-01-01", 5.00, 25.00, 0.50, 6.25, 10.00),
    ("claude-opus-4-6", "2026-01-01", 5.00, 25.00, 0.50, 6.25, 10.00),
    ("claude-opus-4-5", "2026-01-01", 5.00, 25.00, 0.50, 6.25, 10.00),
    ("claude-fable-5", "2026-01-01", 10.00, 50.00, 1.00, 12.50, 20.00),
    # Standard rates, not the published introductory $2/$10. Checked against 37 real
    # runs on 2026-08-17: with token counts matching the runtime exactly, the intro rate
    # put every run 33% under what the runtime billed and the standard rate lands within
    # a few percent. Whatever the intro pricing applies to, it is not what these runs
    # were charged — so the table records what was observed, not what was advertised.
    ("claude-sonnet-5", "2026-01-01", 3.00, 15.00, 0.30, 3.75, 6.00),
    ("claude-sonnet-4-6", "2026-01-01", 3.00, 15.00, 0.30, 3.75, 6.00),
    ("claude-haiku-4-5", "2026-01-01", 1.00, 5.00, 0.10, 1.25, 2.00),
)

# Trailing date snapshots (`-20251001`) and context-window suffixes (`[1m]`) name a
# deployment, not a price tier. Strip them so one price row serves every spelling.
_SUFFIX = re.compile(r"(\[[^\]]*\])?(-\d{8})?$")


def canonical_model(model: str | None) -> str | None:
    return _SUFFIX.sub("", model).strip() or None if model else None


@contextlib.asynccontextmanager
async def connect() -> AsyncIterator[aiosqlite.Connection]:
    conn = await aiosqlite.connect(config.db_path)
    try:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        yield conn
    finally:
        await conn.close()


async def init() -> None:
    async with connect() as conn:
        await conn.executescript(SCHEMA)
        # OR IGNORE, so a price corrected by hand is never overwritten on restart.
        await conn.executemany(
            "INSERT OR IGNORE INTO model_prices VALUES (?, ?, ?, ?, ?, ?, ?)", SEED_PRICES
        )
        await conn.commit()


async def write(rows: Sequence[LlmCall | ToolCall]) -> None:
    """Persist a batch. Called often and mid-run, which is the entire design."""
    llm = [r for r in rows if isinstance(r, LlmCall)]
    tools = [r for r in rows if isinstance(r, ToolCall)]
    if not llm and not tools:
        return
    async with connect() as conn:
        if llm:
            await conn.executemany(
                """INSERT INTO llm_calls
                   (run_id, turn, ts, model, input_tokens, output_tokens,
                    cache_read_tokens, cache_write_5m_tokens, cache_write_1h_tokens,
                    parent_call_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                # Canonicalised on the way in, so the price join is a plain equality and
                # every spelling of a model rolls up to one row in the by-model views.
                [
                    (
                        r.run_id, r.turn, r.ts, canonical_model(r.model), r.input_tokens,
                        r.output_tokens, r.cache_read_tokens, r.cache_write_5m_tokens,
                        r.cache_write_1h_tokens, r.parent_call_id,
                    )
                    for r in llm
                ],
            )
        if tools:
            # REPLACE keyed on the runtime's own tool-use id, so replaying a transcript
            # over rows the live stream already wrote converges instead of duplicating.
            await conn.executemany(
                """INSERT OR REPLACE INTO tool_calls
                   (id, run_id, turn, ts, tool, ok, duration_ms, error, detail,
                    parent_call_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        r.id, r.run_id, r.turn, r.ts, r.tool, int(r.ok), r.duration_ms,
                        r.error, r.detail, r.parent_call_id,
                    )
                    for r in tools
                ],
            )
        await conn.commit()


async def clear_run(run_id: str) -> None:
    """Drop a run's rows. Makes re-running a backfill safe."""
    async with connect() as conn:
        await conn.execute("DELETE FROM llm_calls WHERE run_id = ?", (run_id,))
        await conn.execute("DELETE FROM tool_calls WHERE run_id = ?", (run_id,))
        await conn.commit()


# --------------------------------------------------------------------------- reads

# Cost is derived here and nowhere else: every token column joined to the price that was
# in effect when the call happened. `valid_from` is compared against the call's own
# timestamp, so re-pricing the future never rewrites the past. A model with no price row
# contributes zero rather than dropping the row — usage stays visible even when we have
# not told the table what it costs yet.
COST_SQL = """
    COALESCE(
        c.input_tokens          * p.input_per_mtok
      + c.output_tokens         * p.output_per_mtok
      + c.cache_read_tokens     * p.cache_read_per_mtok
      + c.cache_write_5m_tokens * p.cache_write_5m_per_mtok
      + c.cache_write_1h_tokens * p.cache_write_1h_per_mtok, 0) / 1000000.0
"""

PRICE_JOIN = """
    FROM llm_calls c
    LEFT JOIN model_prices p
           ON p.model = c.model
          AND p.valid_from = (
                SELECT MAX(valid_from) FROM model_prices q
                 WHERE q.model = c.model
                   AND q.valid_from <= COALESCE(c.ts, '9999')
              )
"""


async def _rows(sql: str, args: Sequence[Any] = ()) -> list[dict]:
    async with connect() as conn, conn.execute(sql, tuple(args)) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def usage_for_run(run_id: str) -> dict:
    """Everything the rows know about one run. The per-run read endpoint."""
    totals = await _rows(
        f"""
        SELECT COUNT(*)                        AS calls,
               COALESCE(MAX(c.turn), 0)        AS turns,
               COALESCE(SUM(c.input_tokens), 0)          AS input_tokens,
               COALESCE(SUM(c.output_tokens), 0)         AS output_tokens,
               COALESCE(SUM(c.cache_read_tokens), 0)     AS cache_read_tokens,
               COALESCE(SUM(c.cache_write_5m_tokens
                          + c.cache_write_1h_tokens), 0) AS cache_write_tokens,
               COALESCE(SUM({COST_SQL}), 0)              AS derived_cost_usd
        {PRICE_JOIN}
        WHERE c.run_id = ?
        """,
        (run_id,),
    )
    by_model = await _rows(
        f"""
        SELECT c.model,
               COUNT(*) AS calls,
               COALESCE(SUM({COST_SQL}), 0) AS derived_cost_usd
        {PRICE_JOIN}
        WHERE c.run_id = ?
        GROUP BY c.model ORDER BY derived_cost_usd DESC
        """,
        (run_id,),
    )
    tools = await _rows(
        """
        SELECT tool,
               COUNT(*)                       AS calls,
               SUM(ok = 0)                    AS failures,
               COALESCE(SUM(duration_ms), 0)  AS duration_ms
        FROM tool_calls WHERE run_id = ?
        GROUP BY tool ORDER BY duration_ms DESC
        """,
        (run_id,),
    )
    return {"totals": totals[0] if totals else {}, "by_model": by_model, "tools": tools}


async def spend_by_day(days: int = 30) -> list[dict]:
    return await _rows(
        f"""
        SELECT SUBSTR(c.ts, 1, 10)     AS day,
               SUM({COST_SQL})         AS derived_cost_usd,
               COUNT(DISTINCT c.run_id) AS runs
        {PRICE_JOIN}
        WHERE c.ts IS NOT NULL
        GROUP BY day ORDER BY day DESC LIMIT ?
        """,
        (days,),
    )


async def cost_composition() -> dict:
    """Where the money actually goes, by token class.

    The reason this exists: the run analysis of 2026-08-12 found cache reads were 81% of
    spend, and the old schema had no column that could have shown it.
    """
    rows = await _rows(
        f"""
        SELECT COALESCE(SUM(c.input_tokens * p.input_per_mtok), 0)                   AS input,
               COALESCE(SUM(c.output_tokens * p.output_per_mtok), 0)                 AS output,
               COALESCE(SUM(c.cache_read_tokens * p.cache_read_per_mtok), 0)         AS cache_read,
               COALESCE(SUM(c.cache_write_5m_tokens * p.cache_write_5m_per_mtok
                          + c.cache_write_1h_tokens * p.cache_write_1h_per_mtok), 0) AS cache_write
        {PRICE_JOIN}
        """
    )
    return {k: v / 1_000_000.0 for k, v in (rows[0] if rows else {}).items()}


async def tool_leaderboard(limit: int = 15) -> list[dict]:
    return await _rows(
        """
        SELECT tool,
               COUNT(*)                      AS calls,
               SUM(ok = 0)                   AS failures,
               COALESCE(SUM(duration_ms), 0) AS duration_ms
        FROM tool_calls
        GROUP BY tool ORDER BY duration_ms DESC LIMIT ?
        """,
        (limit,),
    )


async def unit_economics() -> list[dict]:
    """Cost per issue, and how much of it landed.

    The number the old ledger could not produce. An issue is the unit of work, not a
    run: a single issue may cost a build, two retries, a review and a fix, and only the
    sum of those divided by outcomes that actually merged is the factory's unit price.
    `wasted` is the same figure from the other side — spend on runs that never shipped.

    Joins `runs`, which this layer reads and never writes.
    """
    return await _rows(
        f"""
        WITH run_cost AS (
            SELECT c.run_id, SUM({COST_SQL}) AS cost
            {PRICE_JOIN}
            GROUP BY c.run_id
        )
        SELECT r.repo,
               COUNT(DISTINCT r.issue_number)                        AS issues,
               COUNT(*)                                              AS runs,
               COALESCE(SUM(rc.cost), 0)                             AS spend,
               COUNT(DISTINCT CASE WHEN r.pr_url IS NOT NULL
                                   THEN r.issue_number END)          AS shipped,
               COALESCE(SUM(CASE WHEN r.pr_url IS NULL
                                 THEN rc.cost ELSE 0 END), 0)        AS wasted
        FROM runs r
        LEFT JOIN run_cost rc ON rc.run_id = r.id
        GROUP BY r.repo ORDER BY spend DESC
        """
    )
