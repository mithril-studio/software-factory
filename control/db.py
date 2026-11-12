"""SQLite state: one row per run, one row per watched repo.

The `runs` table keeps every column the old pipeline wrote (`kind`, `attempt`, `cycle`,
`verdict`, ...). v1 only writes builds, but `telemetry/` joins `runs` on those columns and an
existing database carries history in them, so they stay. `init()` is idempotent and only ever
adds columns.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import sqlite3
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
    attempt         INTEGER NOT NULL DEFAULT 1,
    cycle           INTEGER NOT NULL DEFAULT 1,
    kind            TEXT NOT NULL DEFAULT 'build',
    verdict         TEXT,
    manifest        TEXT,
    agent           TEXT,
    tokens_in       INTEGER,
    tokens_out      INTEGER,
    cost_usd        REAL,
    base_sha        TEXT,
    over_budget     INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    started_at      TEXT,
    finished_at     TEXT
);
CREATE INDEX IF NOT EXISTS runs_created_idx ON runs (created_at DESC);
CREATE INDEX IF NOT EXISTS runs_status_idx  ON runs (status);

CREATE TABLE IF NOT EXISTS repos (
    repo        TEXT PRIMARY KEY,
    added_at    TEXT NOT NULL,
    base_branch TEXT
);
"""

# Columns a database created by an older version may lack. Only "duplicate column" is
# tolerated; any other error is a real problem and is raised.
MIGRATIONS = (
    "ALTER TABLE runs ADD COLUMN verification_plan TEXT",
    "ALTER TABLE runs ADD COLUMN verification TEXT",
    "ALTER TABLE runs ADD COLUMN base_sha TEXT",
    "ALTER TABLE runs ADD COLUMN over_budget INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE repos ADD COLUMN base_branch TEXT",
)

# Terminal states. Anything else means the run is still in flight.
TERMINAL = ("succeeded", "failed", "cancelled")


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


@contextlib.asynccontextmanager
async def connect() -> AsyncIterator[aiosqlite.Connection]:
    """One short-lived connection per operation."""
    conn = await aiosqlite.connect(settings.db_path, timeout=30)
    try:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        yield conn
    finally:
        await conn.close()


async def init() -> None:
    async with connect() as conn:
        await conn.executescript(SCHEMA)
        for statement in MIGRATIONS:
            try:
                await conn.execute(statement)
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc):
                    raise
        await conn.commit()


def _terminal_marks() -> str:
    return ", ".join("?" for _ in TERMINAL)


# --------------------------------------------------------------------------- runs


async def claim_run(**fields: Any) -> bool:
    """Insert a run row unless the repo already has a run in flight. True if inserted.

    One statement, so two callers racing for the same repo cannot both win: the check and the
    insert happen inside SQLite, not across an `await`.
    """
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    async with connect() as conn:
        cur = await conn.execute(
            f"INSERT INTO runs ({cols}) SELECT {marks} WHERE NOT EXISTS ("
            f"SELECT 1 FROM runs WHERE repo = ? AND status NOT IN ({_terminal_marks()}))",
            (*fields.values(), fields["repo"], *TERMINAL),
        )
        await conn.commit()
        return cur.rowcount > 0


async def update_run(run_id: str, **fields: Any) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    async with connect() as conn:
        await conn.execute(f"UPDATE runs SET {sets} WHERE id = ?", (*fields.values(), run_id))
        await conn.commit()


async def finish_run(run_id: str, status: str, **fields: Any) -> bool:
    """Move a run to a terminal state, once. False if it was already terminal.

    Conditional, so a cancel and a finishing run cannot overwrite each other's outcome.
    """
    assert status in TERMINAL, status
    fields = {"status": status, "finished_at": utcnow(), **fields}
    sets = ", ".join(f"{k} = ?" for k in fields)
    async with connect() as conn:
        cur = await conn.execute(
            f"UPDATE runs SET {sets} WHERE id = ? AND status NOT IN ({_terminal_marks()})",
            (*fields.values(), run_id, *TERMINAL),
        )
        await conn.commit()
        return cur.rowcount > 0


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


async def active_runs() -> list[dict]:
    async with connect() as conn:
        async with conn.execute(
            f"SELECT * FROM runs WHERE status NOT IN ({_terminal_marks()}) ORDER BY created_at",
            TERMINAL,
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def has_active_run(repo: str) -> bool:
    async with connect() as conn:
        async with conn.execute(
            f"SELECT 1 FROM runs WHERE repo = ? AND status NOT IN ({_terminal_marks()}) LIMIT 1",
            (repo, *TERMINAL),
        ) as cur:
            return await cur.fetchone() is not None


async def failed_vm_names(since: str) -> set[str]:
    """VM names of runs that failed since `since`: the ones FACTORY_KEEP_FAILED keeps."""
    async with connect() as conn:
        async with conn.execute(
            "SELECT vm_name FROM runs WHERE status = 'failed' AND finished_at >= ? "
            "AND vm_name IS NOT NULL",
            (since,),
        ) as cur:
            return {r["vm_name"] for r in await cur.fetchall()}


async def stats_by_repo() -> dict[str, dict]:
    """Per-repo run tallies for the Projects page. Keyed by repo."""
    async with connect() as conn:
        async with conn.execute(
            f"""
            SELECT repo,
                   COUNT(*)                                    AS runs,
                   SUM(status = 'succeeded')                   AS succeeded,
                   SUM(status = 'failed')                      AS failed,
                   SUM(status NOT IN ({_terminal_marks()}))    AS active,
                   MAX(created_at)                             AS last_run
            FROM runs GROUP BY repo
            """,
            TERMINAL,
        ) as cur:
            rows = await cur.fetchall()
    return {r["repo"]: dict(r) for r in rows}


# --------------------------------------------------------------------------- repos


async def add_repo(repo: str) -> None:
    """Register a repo, or leave an already-registered one as it is."""
    async with connect() as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO repos (repo, added_at) VALUES (?, ?)", (repo, utcnow())
        )
        await conn.commit()


async def remove_repo(repo: str) -> bool:
    """Stop watching `repo`. Its runs stay: they are the ledger."""
    async with connect() as conn:
        cur = await conn.execute("DELETE FROM repos WHERE repo = ?", (repo,))
        await conn.commit()
        return cur.rowcount > 0


async def list_repos() -> list[dict]:
    """Every watched repo, oldest first, so the poller works them in the order they arrived."""
    async with connect() as conn:
        async with conn.execute(
            "SELECT repo, added_at, base_branch FROM repos ORDER BY added_at, repo"
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def set_repo_base_branch(repo: str, branch: str | None) -> None:
    """Record which branch `repo`'s pull requests target. None reverts to the default."""
    async with connect() as conn:
        await conn.execute("UPDATE repos SET base_branch = ? WHERE repo = ?", (branch, repo))
        await conn.commit()
