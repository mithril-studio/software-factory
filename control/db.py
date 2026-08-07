"""Run storage.

SQLite for now. The schema is deliberately plain SQL so moving to Postgres later is a
driver swap plus a handful of placeholder changes, not a rewrite.
"""

from __future__ import annotations

import contextlib
import datetime as dt
from collections.abc import AsyncIterator
from typing import Any

import aiosqlite

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id              TEXT PRIMARY KEY,
    repo            TEXT NOT NULL,
    issue_number    INTEGER NOT NULL,
    issue_title     TEXT,
    branch          TEXT,
    golden          TEXT,
    vm_name         TEXT,
    vm_id           TEXT,
    status          TEXT NOT NULL,
    exit_code       INTEGER,
    pr_url          TEXT,
    error           TEXT,
    log_path        TEXT,
    transcript_path TEXT,
    created_at      TEXT NOT NULL,
    started_at      TEXT,
    finished_at     TEXT
);
CREATE INDEX IF NOT EXISTS runs_created_idx ON runs (created_at DESC);
CREATE INDEX IF NOT EXISTS runs_status_idx  ON runs (status);
"""

# Terminal states. Anything else means the run is still in flight.
TERMINAL = ("succeeded", "failed", "cancelled")


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


@contextlib.asynccontextmanager
async def connect() -> AsyncIterator[aiosqlite.Connection]:
    """One short-lived connection per operation.

    aiosqlite connections are backed by a thread each, and a Connection may only be
    awaited once — so this is a context manager rather than a coroutine returning a live
    handle. At this scale the per-call cost is irrelevant and it keeps concurrent tasks
    from sharing a connection.
    """
    conn = await aiosqlite.connect(settings.db_path)
    try:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        yield conn
    finally:
        await conn.close()


async def init() -> None:
    async with connect() as conn:
        await conn.executescript(SCHEMA)
        await conn.commit()


async def create_run(**fields: Any) -> None:
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    async with connect() as conn:
        await conn.execute(f"INSERT INTO runs ({cols}) VALUES ({marks})", tuple(fields.values()))
        await conn.commit()


async def update_run(run_id: str, **fields: Any) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    async with connect() as conn:
        await conn.execute(
            f"UPDATE runs SET {sets} WHERE id = ?", (*fields.values(), run_id)
        )
        await conn.commit()


async def get_run(run_id: str) -> dict | None:
    async with connect() as conn:
        async with conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def list_runs(limit: int = 100) -> list[dict]:
    async with connect() as conn:
        async with conn.execute(
            "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def has_active_run(repo: str) -> bool:
    """True if `repo` already has a non-terminal run.

    This is the dispatch guard. The poller checks it before claiming, so a repo runs one
    issue at a time (lowest number first) and a slow label write can never cause a double
    dispatch — the database, not the issue label, decides what is already in flight.
    """
    marks = ", ".join("?" for _ in TERMINAL)
    async with connect() as conn:
        async with conn.execute(
            f"SELECT 1 FROM runs WHERE repo = ? AND status NOT IN ({marks}) LIMIT 1",
            (repo, *TERMINAL),
        ) as cur:
            return await cur.fetchone() is not None


async def active_runs() -> list[dict]:
    marks = ", ".join("?" for _ in TERMINAL)
    async with connect() as conn:
        async with conn.execute(
            f"SELECT * FROM runs WHERE status NOT IN ({marks}) ORDER BY created_at", TERMINAL
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]
