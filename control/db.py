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
    attempt         INTEGER NOT NULL DEFAULT 1,
    kind            TEXT NOT NULL DEFAULT 'build',
    verdict         TEXT,
    manifest        TEXT,
    agent           TEXT,
    tokens_in       INTEGER,
    tokens_out      INTEGER,
    cost_usd        REAL,
    created_at      TEXT NOT NULL,
    started_at      TEXT,
    finished_at     TEXT
);
CREATE INDEX IF NOT EXISTS runs_created_idx ON runs (created_at DESC);
CREATE INDEX IF NOT EXISTS runs_status_idx  ON runs (status);

-- Every golden snapshot the fleet holds, one row per name. An observation of a thing that
-- exists, not an event log — fleet state belongs in a table for the same reason run state
-- does, otherwise "which goldens can this deployment boot?" is answered by somebody listing
-- snapshots and remembering.
--
-- `repo` is the repo slug the name carries, NULL for the base image. `agent` is which agent
-- the image announced in its manifest on the way into its last run — a fact reported by the
-- snapshot, not one derived from its name, which is the whole change this table's rename
-- records.
--
-- Named `snapshots` rather than `goldens` for a dull but load-bearing reason: `goldens` is a
-- table this schema already drops below, and `executescript` runs before the migrations do,
-- so a table created here under that name would be created and then immediately dropped on
-- every start.
--
-- Two clocks, deliberately far apart. `checked_at` is when the refresh loop last saw the
-- name in the fleet, which costs a list call and says only that the snapshot is there.
-- `verified_at` is when a run last finished on it having produced usage — the only evidence
-- that its credentials still work, and unlike a probe it is free, because the runs were
-- happening anyway.
CREATE TABLE IF NOT EXISTS snapshots (
    name          TEXT PRIMARY KEY,
    repo          TEXT,
    agent         TEXT,
    version       TEXT,
    events        TEXT,
    transcript    TEXT,
    manifest      TEXT,
    agent_version TEXT,
    ok            INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    last_run      TEXT,
    verified_at   TEXT,
    checked_at    TEXT NOT NULL
);
"""

# Additive migrations for databases created before a column existed. Each is tried once at
# startup and the "duplicate column" error on an already-migrated database is ignored.
MIGRATIONS = (
    "ALTER TABLE runs ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE runs ADD COLUMN agent TEXT",
    "ALTER TABLE runs ADD COLUMN tokens_in INTEGER",
    "ALTER TABLE runs ADD COLUMN tokens_out INTEGER",
    "ALTER TABLE runs ADD COLUMN cost_usd REAL",
    # 'build' (an agent resolving an issue) or 'review' (an agent checking the resulting PR
    # against the issue's acceptance criteria). Both are runs on a forked VM, which is why
    # they share this table rather than getting one of their own.
    "ALTER TABLE runs ADD COLUMN kind TEXT NOT NULL DEFAULT 'build'",
    # Review runs only: the verdict JSON the reviewing agent produced.
    "ALTER TABLE runs ADD COLUMN verdict TEXT",
    # What the golden said about itself on the way into the run: which agent it launches,
    # where that agent writes its transcript, which telemetry shape it emits. Stored raw
    # rather than spread across columns, because the control plane is not the authority on
    # what a manifest may contain — the golden is, and it gains keys without a migration.
    "ALTER TABLE runs ADD COLUMN manifest TEXT",
    # The freshness sweep's table. It held one row per golden *machine*: how far behind its
    # checkout was, whether a dependency manifest had moved. A repo-agnostic snapshot has no
    # checkout, so every column of it became unanswerable at once — dropped rather than left
    # to be read by something that has forgotten it stopped being filled in.
    "DROP TABLE IF EXISTS goldens",
    # Its replacement, and the same reasoning one design later. The `agents` table was keyed
    # on snapshot name with an `agent` column filled in *from that name* — so it recorded the
    # naming convention rather than anything observed. Goldens are named for the repo now and
    # the agent comes from the image's own manifest; `snapshots` above holds both. Dropped
    # rather than migrated because every row is a cache of the last fleet listing, rebuilt on
    # the next refresh.
    "DROP TABLE IF EXISTS agents",
)

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
        for statement in MIGRATIONS:
            try:
                await conn.execute(statement)
            except Exception:  # noqa: BLE001 - already applied; the column exists
                pass
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


async def record_snapshot(name: str, **fields: Any) -> None:
    """Store what the refresh saw. One row per golden snapshot, replaced each time."""
    fields = {"name": name, **fields}
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    async with connect() as conn:
        await conn.execute(
            f"INSERT OR REPLACE INTO snapshots ({cols}) VALUES ({marks})", tuple(fields.values())
        )
        await conn.commit()


async def snapshots() -> dict[str, dict]:
    """The last refresh's findings, keyed by snapshot name."""
    async with connect() as conn:
        async with conn.execute("SELECT * FROM snapshots") as cur:
            rows = await cur.fetchall()
    return {r["name"]: dict(r) for r in rows}


async def snapshot_evidence() -> dict[str, dict]:
    """What the runs already prove about each golden, keyed by snapshot name.

    This is the whole grading strategy: a golden is not asked how it is, it is judged by
    what happened on it. The expensive failure on these snapshots is credential expiry, and
    the only real test of a credential is using one — which every run does anyway, for free,
    on the machine the question is about.

    Two different facts, and the difference matters. `ok`/`error`/`last_run` come from the
    most recent run that reached an end on it, so a golden whose last run failed says so.
    `verified_at` is the most recent run that also *produced usage* — an agent that emitted
    tokens authenticated, whatever the run then did with the code.
    """
    async with connect() as conn:
        async with conn.execute(
            "SELECT golden, id, status, error, finished_at, manifest, tokens_out, cost_usd "
            "FROM runs "
            "WHERE golden IS NOT NULL AND golden != '' AND finished_at IS NOT NULL "
            "ORDER BY finished_at DESC"
        ) as cur:
            rows = await cur.fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        seen = out.setdefault(r["golden"], {})
        if "last_run" not in seen:
            seen.update(
                last_run=r["id"],
                ok=1 if r["status"] == "succeeded" else 0,
                error=r["error"] if r["status"] != "succeeded" else None,
                manifest=r["manifest"],
            )
        if "verified_at" not in seen and ((r["tokens_out"] or 0) > 0 or r["cost_usd"] is not None):
            seen["verified_at"] = r["finished_at"]
    return out


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


async def stats_by_repo() -> dict[str, dict]:
    """Per-repo run tallies for the Projects page. Keyed by repo."""
    marks = ", ".join("?" for _ in TERMINAL)
    async with connect() as conn:
        async with conn.execute(
            f"""
            SELECT repo,
                   COUNT(*)                                        AS runs,
                   SUM(status = 'succeeded')                       AS succeeded,
                   SUM(status = 'failed')                          AS failed,
                   SUM(status NOT IN ({marks}))                    AS active,
                   MAX(created_at)                                 AS last_run
            FROM runs GROUP BY repo
            """,
            TERMINAL,
        ) as cur:
            rows = await cur.fetchall()
    return {r["repo"]: dict(r) for r in rows}


async def active_runs() -> list[dict]:
    marks = ", ".join("?" for _ in TERMINAL)
    async with connect() as conn:
        async with conn.execute(
            f"SELECT * FROM runs WHERE status NOT IN ({marks}) ORDER BY created_at", TERMINAL
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]
