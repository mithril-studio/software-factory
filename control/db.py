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
    cycle           INTEGER NOT NULL DEFAULT 1,
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
-- The repos this deployment watches. Connecting one used to mean editing FACTORY_REPOS in
-- `.env` on the box and restarting systemd, which is not a thing a web interface can do — so
-- the register moved into the database and `FACTORY_REPOS` became the seed for it.
--
-- `golden` and `provision_status` are about the *warm tier* only, and nothing gates on them.
-- A repo with no golden of its own dispatches onto `golden-copy` and installs for itself, so
-- provisioning may still be running, may have failed, or may never have been started, and the
-- repo works either way. These columns say what happened, not whether it may run.
--
-- `agent` is nullable and always NULL today. Kept because a second agent needs somewhere to
-- record which one a repo uses, and adding the column later is a migration for something that
-- costs nothing now — but nothing reads it, and nothing should until a second base image
-- exists to choose between.
CREATE TABLE IF NOT EXISTS repos (
    repo             TEXT PRIMARY KEY,
    added_at         TEXT NOT NULL,
    golden           TEXT,
    provision_status TEXT NOT NULL DEFAULT 'none',
    agent            TEXT
);

CREATE TABLE IF NOT EXISTS snapshots (
    name          TEXT PRIMARY KEY,
    repo          TEXT,
    agent         TEXT,
    version       TEXT,
    status        TEXT,
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

-- Admission control between an agent noticing something and the factory treating it as
-- durable truth. A candidate is evidence-backed, scoped to the run and repo that produced
-- it, and sits in `pending` until something (not this table) decides to accept or reject it.
-- Nothing here writes to a repo's own `.mem/` — that stays a later step's job.
CREATE TABLE IF NOT EXISTS memory_candidates (
    id          TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    repo        TEXT NOT NULL,
    domain      TEXT NOT NULL,
    type        TEXT NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT,
    evidence    TEXT,
    confidence  TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS memory_candidates_repo_idx ON memory_candidates (repo);
CREATE INDEX IF NOT EXISTS memory_candidates_status_idx ON memory_candidates (status);
"""

# Additive migrations for databases created before a column existed. Each is tried once at
# startup and the "duplicate column" error on an already-migrated database is ignored.
MIGRATIONS = (
    "ALTER TABLE runs ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE runs ADD COLUMN agent TEXT",
    "ALTER TABLE runs ADD COLUMN tokens_in INTEGER",
    "ALTER TABLE runs ADD COLUMN tokens_out INTEGER",
    "ALTER TABLE runs ADD COLUMN cost_usd REAL",
    # 'build' (an agent resolving an issue), 'review' (an agent checking the resulting PR
    # against the issue's acceptance criteria), or 'provision' (no agent at all — clone and
    # install a repo into its own golden snapshot). All three are runs on a VM restored from a
    # golden, which is why they share this table rather than getting one each: they want the
    # same streamed log, the same cancel, the same reaper.
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
    # What boxd says the snapshot is doing. `pending` with no version is a capture still being
    # written and nothing can boot it; `pending` with one is a re-save, and the older version
    # stays restorable. The distinction cost a run to learn.
    "ALTER TABLE snapshots ADD COLUMN status TEXT",
    # Which review cycle a run belongs to. `attempt` used to carry both numbers: a crash
    # retry incremented it, and so did a fix run sent back by a review — so the runs list
    # said "try 2" about a build whose first try had succeeded, and the issue comment
    # counted a fix cycle against `max_attempts` when the budget that governs it is
    # `max_review_cycles`. Two questions, two columns: `attempt` is how many times this
    # build has been dispatched inside its cycle, `cycle` is which pass over the pull
    # request it belongs to.
    "ALTER TABLE runs ADD COLUMN cycle INTEGER NOT NULL DEFAULT 1",
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


# Which runs are the same piece of work. A build, the CI that judged what it pushed and the
# review of that same pull request are three dispatches and one attempt at an issue — so the
# dispatch log used to show them as three unrelated rows in reverse time order, and a build
# sat there reading `succeeded` directly above the review that had sent it back. `cycle` is
# what makes the key work: crash retries of one cycle group together, and a fix cycle opens
# a new group rather than reopening the old one.
#
# A run with no issue behind it is its own group. Provisioning runs all carry `issue_number`
# 0, so grouping them the same way would collapse every golden a repo ever built into a
# single row that claimed to be one attempt.
ATTEMPT_KEY = (
    "CASE WHEN issue_number > 0 "
    "THEN repo || '#' || issue_number || '/' || COALESCE(cycle, 1) "
    "ELSE id END"
)


async def list_attempts(limit: int = 60) -> list[dict]:
    """The run log grouped into attempts: newest attempt first, phases in causal order.

    Paginates on *groups*, which is the whole reason this is a query rather than a groupBy in
    the browser. Fetching the newest N runs and grouping them there cuts whichever attempt
    straddles the boundary in half, and the surviving half is the one that reads wrong — a
    build with no review under it looks like work that succeeded and stopped.

    Phases ascend by `created_at` because the interesting reading is causal: what was built,
    what CI made of it, what the reviewer then decided. The list as a whole stays newest
    first, so the two orders are deliberately opposite.
    """
    async with connect() as conn:
        async with conn.execute(
            f"SELECT {ATTEMPT_KEY} AS k, MAX(created_at) AS last_at "
            "FROM runs GROUP BY k ORDER BY last_at DESC LIMIT ?",
            (limit,),
        ) as cur:
            keys = [r["k"] for r in await cur.fetchall()]
        if not keys:
            return []
        marks = ", ".join("?" for _ in keys)
        async with conn.execute(
            f"SELECT *, {ATTEMPT_KEY} AS attempt_key FROM runs "
            f"WHERE {ATTEMPT_KEY} IN ({marks}) ORDER BY created_at ASC",
            tuple(keys),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row.pop("attempt_key"), []).append(row)
    # `keys` carries the newest-first order the first query established; the second query
    # threw it away by ordering on time ascending. Rebuild from `keys`, never from `grouped`.
    return [{"key": k, "phases": grouped[k]} for k in keys if k in grouped]


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


# --------------------------------------------------------------------------- repos


async def add_repo(repo: str, agent: str | None = None) -> None:
    """Register a repo, or leave an already-registered one exactly as it is.

    `INSERT OR IGNORE` rather than `OR REPLACE`: re-adding a repo must not reset the golden it
    has already been provisioned, and the seed from `FACTORY_REPOS` runs on every boot.
    """
    async with connect() as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO repos (repo, added_at, agent) VALUES (?, ?, ?)",
            (repo, utcnow(), agent),
        )
        await conn.commit()


async def remove_repo(repo: str) -> bool:
    """Stop watching `repo`. Returns whether there was anything to remove.

    The runs stay. They are the ledger of what this deployment spent and shipped, and a repo
    being disconnected does not make its history untrue — `stats_by_repo` simply stops being
    asked about it.
    """
    async with connect() as conn:
        cur = await conn.execute("DELETE FROM repos WHERE repo = ?", (repo,))
        await conn.commit()
        return cur.rowcount > 0


async def list_repos() -> list[dict]:
    """Every watched repo, oldest first, so the poller works them in the order they arrived."""
    async with connect() as conn:
        async with conn.execute("SELECT * FROM repos ORDER BY added_at, repo") as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def set_repo_golden(repo: str, golden: str | None, status: str) -> None:
    """Record what provisioning a golden for `repo` did.

    Separate from `add_repo` because it happens much later and from a different task: a repo is
    watched the moment it is connected, and its golden arrives whenever the provisioning run
    finishes — or does not arrive at all, which is a slower repo and not a broken one.
    """
    async with connect() as conn:
        await conn.execute(
            "UPDATE repos SET golden = ?, provision_status = ? WHERE repo = ?",
            (golden, status, repo),
        )
        await conn.commit()


async def has_active_run(repo: str) -> bool:
    """True if `repo` already has a non-terminal run.

    This is the dispatch guard. The poller checks it before claiming, so a repo runs one
    issue at a time (lowest number first) and a slow label write can never cause a double
    dispatch — the database, not the issue label, decides what is already in flight.

    Provisioning runs are excluded. They are runs in every other sense — a VM, a streamed
    log, a cancel button — but they claim no issue, and counting them here made connecting a
    repo stop it: `POST /api/repos` starts a warm-up immediately, so a repo arriving with
    queued issues sat idle for the whole install. Nothing about a golden gates dispatch, and
    that is the point of the two-tier design: a repo with no warm snapshot boots `golden-copy`
    and installs for itself, including while its own snapshot is being built.
    """
    marks = ", ".join("?" for _ in TERMINAL)
    async with connect() as conn:
        async with conn.execute(
            f"SELECT 1 FROM runs WHERE repo = ? AND kind != 'provision' "
            f"AND status NOT IN ({marks}) LIMIT 1",
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


# --------------------------------------------------------------------------- memory candidates

# The only transitions a pending candidate may make. Both destinations are terminal — neither
# key appears on the left below, so a second transition of any kind is rejected rather than
# silently accepted.
CANDIDATE_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "pending": ("accepted", "rejected"),
}

CANDIDATE_INITIAL_STATUS = "pending"

# The columns a caller may set. Values are always bound as parameters, but column *names* are
# interpolated into the statement — there is no placeholder for an identifier in SQL — so the
# set of legal names has to be closed here rather than taken from whatever the caller passed.
# It matters because of where candidates come from: a run collects them from a JSONL file the
# agent wrote, and an unknown key would otherwise travel from that file straight into the text
# of a statement. A typo becomes a clear error at the same time.
CANDIDATE_COLUMNS = frozenset({
    "id", "run_id", "repo", "domain", "type", "title", "body", "evidence",
    "confidence", "status", "created_at", "updated_at",
})


async def create_candidate(**fields: Any) -> None:
    """Insert a candidate, or leave an already-inserted one exactly as it is.

    `INSERT OR IGNORE` on the primary key: resubmitting the same candidate id (an agent
    retrying, or a run observing the same evidence twice) must not duplicate it or reset a
    status a reviewer has already moved on from.

    Raises `ValueError` on an unknown column, or on a status this table does not define.
    """
    fields = {"status": CANDIDATE_INITIAL_STATUS, **fields}
    unknown = sorted(set(fields) - CANDIDATE_COLUMNS)
    if unknown:
        raise ValueError(f"unknown memory_candidates column(s): {', '.join(unknown)}")
    # Every candidate is born pending. `transition_candidate` is the only door to a terminal
    # state, and it is the door that enforces "exactly once" — so letting an insert name its
    # own status would be a way to arrive at `accepted` having skipped triage entirely, which
    # is the one thing this queue exists to prevent.
    if fields["status"] != CANDIDATE_INITIAL_STATUS:
        raise ValueError(
            f"a candidate is created {CANDIDATE_INITIAL_STATUS}, not {fields['status']!r}; "
            f"use transition_candidate"
        )
    now = utcnow()
    fields.setdefault("created_at", now)
    fields.setdefault("updated_at", now)
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    async with connect() as conn:
        await conn.execute(
            f"INSERT OR IGNORE INTO memory_candidates ({cols}) VALUES ({marks})",
            tuple(fields.values()),
        )
        await conn.commit()


async def get_candidate(candidate_id: str) -> dict | None:
    async with connect() as conn:
        async with conn.execute(
            "SELECT * FROM memory_candidates WHERE id = ?", (candidate_id,)
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def list_candidates(repo: str | None = None, status: str | None = None) -> list[dict]:
    """Candidates oldest first, optionally scoped to a repo and/or a status."""
    clauses = []
    params: list[Any] = []
    if repo is not None:
        clauses.append("repo = ?")
        params.append(repo)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    async with connect() as conn:
        async with conn.execute(
            f"SELECT * FROM memory_candidates{where} ORDER BY created_at", params
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def pending_candidates_by_repo() -> dict[str, int]:
    """How many undecided candidates each repo is holding. Keyed by repo, absent when zero."""
    async with connect() as conn:
        async with conn.execute(
            "SELECT repo, COUNT(*) AS pending FROM memory_candidates "
            "WHERE status = ? GROUP BY repo",
            (CANDIDATE_INITIAL_STATUS,),
        ) as cur:
            rows = await cur.fetchall()
    return {r["repo"]: r["pending"] for r in rows}


async def transition_candidate(candidate_id: str, to_status: str) -> dict:
    """Move a candidate along an explicitly allowed edge, or raise.

    Every candidate starts `pending`; `accepted` and `rejected` are terminal, so this is the
    only place a status ever changes. Raising rather than returning a bool means a caller
    cannot mistake "already there" for "just happened" — the two are different bugs.
    """
    candidate = await get_candidate(candidate_id)
    if candidate is None:
        raise ValueError(f"no such candidate: {candidate_id}")
    current = candidate["status"]
    allowed = CANDIDATE_TRANSITIONS.get(current, ())
    if to_status not in allowed:
        raise ValueError(f"cannot transition candidate {candidate_id} from {current} to {to_status}")
    now = utcnow()
    async with connect() as conn:
        # `WHERE status = ?` as well as `WHERE id = ?`, and the row count is the answer. The
        # read above cannot be what decides: two reviewers clicking accept and reject at the
        # same moment both read `pending`, and an unconditional UPDATE would let both "succeed"
        # with the slower one silently overwriting the faster. The database decides who was
        # first; the loser is told, and gets the same error as any other invalid transition.
        cursor = await conn.execute(
            "UPDATE memory_candidates SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
            (to_status, now, candidate_id, current),
        )
        await conn.commit()
        if not cursor.rowcount:
            raise ValueError(
                f"cannot transition candidate {candidate_id} from {current} to {to_status}: "
                f"it changed underneath this request"
            )
    candidate["status"] = to_status
    candidate["updated_at"] = now
    return candidate
