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
import json
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

CREATE TABLE IF NOT EXISTS memory_reads (
    run_id      TEXT NOT NULL,
    memory_id   TEXT NOT NULL,
    ts          TEXT,
    PRIMARY KEY (run_id, memory_id)
);
CREATE INDEX IF NOT EXISTS memory_reads_run_idx ON memory_reads (run_id);

-- The receipt as the run gave it. `domains` is a JSON array because that is the shape the
-- agent reports: which domain files the run drew from, as a set with no mapping back to
-- individual records. There used to be a `domain` column on `memory_reads` filled by picking
-- the first of them, which wrote every record as whichever domain sorted first and lost the
-- rest. A record's domain is a property of the record — `index.jsonl` requires it — so a copy
-- here could only ever be a second answer that disagrees. Stored at run level, which is also
-- exactly what AC2 asks for, it is simply true.
CREATE TABLE IF NOT EXISTS memory_receipts (
    run_id      TEXT PRIMARY KEY,
    indexed     INTEGER,
    domains     TEXT,
    received_at TEXT
);

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
    if not model:
        return None
    return _SUFFIX.sub("", model).strip() or None


@contextlib.asynccontextmanager
async def connect() -> AsyncIterator[aiosqlite.Connection]:
    conn = await aiosqlite.connect(config.db_path)
    try:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        yield conn
    finally:
        await conn.close()


# Changes to tables that already exist on a deployed box. SQLite has no `IF EXISTS` for a
# column drop, so each statement is tried and a failure means it was already applied — the
# only two outcomes here are "the old column was there" and "it was not".
#
# Applied strictly *after* SCHEMA, and the ordering is the point: SCHEMA creates tables in
# their current shape, so a migration that dropped something SCHEMA still created would undo
# it on every single boot. Read the pair together before adding to either.
MIGRATIONS = (
    "ALTER TABLE memory_reads DROP COLUMN domain",
)


async def init() -> None:
    async with connect() as conn:
        await conn.executescript(SCHEMA)
        for statement in MIGRATIONS:
            try:
                await conn.execute(statement)
            except Exception:  # noqa: BLE001 - already applied is the normal case
                pass
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
    """Drop a run's rows. Makes re-running a backfill safe.

    Every table keyed on `run_id`, memory included. `memory_reads` IGNOREs a repeat and
    `memory_receipts` REPLACEs one, so leaving them behind looked harmless — but only while a
    replay produces exactly the same receipt. A replay that reads a *corrected* transcript, or
    one that finds no receipt at all, would otherwise leave the old rows standing as the run's
    answer forever. "Clear the run" has to mean the run.
    """
    async with connect() as conn:
        await conn.execute("DELETE FROM llm_calls WHERE run_id = ?", (run_id,))
        await conn.execute("DELETE FROM tool_calls WHERE run_id = ?", (run_id,))
        await conn.execute("DELETE FROM memory_reads WHERE run_id = ?", (run_id,))
        await conn.execute("DELETE FROM memory_receipts WHERE run_id = ?", (run_id,))
        await conn.commit()


async def write_memory_reads(run_id: str, reads: Sequence[tuple[str, str | None]]) -> None:
    """Record which memory records a run pulled into context.

    Each item is `(memory_id, ts)` — no domain, because the receipt does not say which
    record came from which file and a guess here is a wrong answer stored as a fact. The
    domains a run drew from go to `write_memory_receipt`, at the level the run reports them.

    Keyed on `(run_id, memory_id)`, so retrieving the same record twice in one run — the
    recorder replaying a transcript, or two turns reaching for the same fact — leaves one
    row rather than accumulating duplicates.
    """
    if not reads:
        return
    async with connect() as conn:
        await conn.executemany(
            "INSERT OR IGNORE INTO memory_reads (run_id, memory_id, ts) VALUES (?, ?, ?)",
            [(run_id, memory_id, ts) for memory_id, ts in reads],
        )
        await conn.commit()


async def write_memory_receipt(
    run_id: str, indexed: int, domains: Sequence[str], ts: str | None = None
) -> None:
    """Record the run-level half of a receipt: how big the index was, which domains it drew from.

    REPLACE rather than IGNORE: a run emits at most one receipt, and a second one is a
    correction of the first — the priming step re-running, not two separate facts.
    """
    async with connect() as conn:
        await conn.execute(
            """INSERT OR REPLACE INTO memory_receipts (run_id, indexed, domains, received_at)
               VALUES (?, ?, ?, ?)""",
            (run_id, int(indexed), json.dumps(list(domains)), ts),
        )
        await conn.commit()


async def memory_reads_for_run(run_id: str) -> list[dict]:
    """Every memory record a run retrieved. The per-run read endpoint for AC1."""
    return await _rows(
        """SELECT memory_id, ts FROM memory_reads
           WHERE run_id = ? ORDER BY ts, memory_id""",
        (run_id,),
    )


async def memory_receipt_for_run(run_id: str) -> dict:
    """A run's receipt, with `domains` decoded back to a list. `{}` when it filed none."""
    rows = await _rows(
        "SELECT indexed, domains, received_at FROM memory_receipts WHERE run_id = ?",
        (run_id,),
    )
    if not rows:
        return {}
    row = dict(rows[0])
    try:
        row["domains"] = json.loads(row["domains"] or "[]")
    except (TypeError, ValueError):
        row["domains"] = []
    return row


async def memory_metrics_by_repo() -> list[dict]:
    """Retrieval rolled up per repository: how many runs used memory, how many distinct
    records, and what those runs cost on average.

    `repo` comes from `runs`, the same table `unit_economics` already joins — a run's
    repository has exactly one source of truth in this system, so this reads it rather
    than inventing a second place memory rows could disagree with it. This proves
    retrieval happened alongside a run's outcome; it says nothing about whether a
    retrieved record was actually useful.
    """
    rows = await _rows(
        f"""
        WITH run_cost AS (
            SELECT c.run_id, SUM({COST_SQL}) AS cost
            {PRICE_JOIN}
            GROUP BY c.run_id
        ),
        -- Every run that reported on memory at all, whether or not it opened anything. A run
        -- that primed an index of 40 records and opened none is the single most useful row in
        -- this table — it is memory failing to earn its keep — and reading only `memory_reads`
        -- made exactly that run invisible, along with every repo whose runs all look like it.
        -- `runs_with_memory` still counts only runs that retrieved a record, because that is
        -- what the phrase means; `runs_primed` counts the ones that got as far as priming.
        repo_runs AS (
            SELECT DISTINCT mr.run_id, r.repo
            FROM memory_reads mr
            JOIN runs r ON r.id = mr.run_id
            UNION
            SELECT DISTINCT mrc.run_id, r.repo
            FROM memory_receipts mrc
            JOIN runs r ON r.id = mrc.run_id
        )
        SELECT rr.repo,
               (SELECT COUNT(DISTINCT mr3.run_id)
                  FROM memory_reads mr3
                  JOIN runs r3 ON r3.id = mr3.run_id
                 WHERE r3.repo = rr.repo)  AS runs_with_memory,
               COUNT(DISTINCT rr.run_id)   AS runs_primed,
               (SELECT COUNT(DISTINCT mr2.memory_id)
                  FROM memory_reads mr2
                  JOIN runs r2 ON r2.id = mr2.run_id
                 WHERE r2.repo = rr.repo)  AS distinct_records,
               COALESCE(AVG(rc.cost), 0)   AS avg_derived_cost_usd
        FROM repo_runs rr
        LEFT JOIN run_cost rc ON rc.run_id = rr.run_id
        GROUP BY rr.repo ORDER BY rr.repo
        """
    )
    # Domains are unioned from the receipts rather than read off each row, because a run
    # reports them as a set over the whole run. Done in Python rather than SQL: the column is
    # a JSON array, and json_each would make this query depend on a SQLite build option for
    # something one pass over a handful of rows does plainly.
    by_repo: dict[str, set[str]] = {}
    for receipt in await _rows(
        """SELECT r.repo AS repo, mrc.domains AS domains
             FROM memory_receipts mrc JOIN runs r ON r.id = mrc.run_id"""
    ):
        try:
            names = json.loads(receipt["domains"] or "[]")
        except (TypeError, ValueError):
            names = []
        by_repo.setdefault(receipt["repo"], set()).update(
            n for n in names if isinstance(n, str)
        )
    for row in rows:
        row["domains"] = sorted(by_repo.get(row["repo"], ()))
    return rows


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
    memory = await memory_reads_for_run(run_id)
    return {
        "totals": totals[0] if totals else {},
        "by_model": by_model,
        "tools": tools,
        "memory": memory,
        "memory_receipt": await memory_receipt_for_run(run_id),
    }


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
