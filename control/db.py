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
--
-- `goal` is the endstate the repo is being built toward — prose, written by a human through
-- the API. It is what makes the factory self-serving: when a repo's queue runs dry and its
-- `goal_state` is 'active', the poller dispatches a plan run that compares the repo against
-- this text and either files the next issues or declares the goal met. `goal_state` is
-- 'none' (no goal), 'active' (planning may fire), 'met' (a plan run verified the endstate
-- and the queue was empty), or 'stalled' (consecutive fruitless plans hit the cap — a human
-- decides what happens next). `plan_stalls` counts those fruitless plans; `last_planned_at`
-- is the cooldown clock between plan dispatches.
CREATE TABLE IF NOT EXISTS repos (
    repo             TEXT PRIMARY KEY,
    added_at         TEXT NOT NULL,
    golden           TEXT,
    provision_status TEXT NOT NULL DEFAULT 'none',
    agent            TEXT,
    goal             TEXT,
    goal_state       TEXT NOT NULL DEFAULT 'none',
    plan_stalls      INTEGER NOT NULL DEFAULT 0,
    last_planned_at  TEXT
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

-- What the improvement loop changed, and why it thought so at the time.
--
-- The loop edits the things that decide how agents behave — a repo's skills, its
-- `.factory.md`, its `.mem/`. Those changes arrive as ordinary pull requests and are reviewed
-- like code, so git already records *what* changed. What git cannot record is the reasoning:
-- which runs were the evidence, which number the change was supposed to move, and what that
-- number actually did afterwards. Without that, a rule merged six weeks ago is indistinguishable
-- from one somebody typed on a hunch, and the only safe thing to do with an unattributable rule
-- is leave it there forever. This table is what makes deletion possible.
--
-- Three readers, and each needs a different column. A human auditing what the loop has been
-- doing reads `rationale` and `evidence`. The grader reads `metric` and `baseline`, and writes
-- `observed`. The next learning run reads the whole history, including the failures — without
-- it the loop re-proposes what it already tried and reverted, and oscillates forever.
--
-- It lives in `control` rather than `telemetry` because it records decisions, not traces:
-- telemetry observes what happened, this says what the factory chose to do about it.
CREATE TABLE IF NOT EXISTS improvements (
    id          TEXT PRIMARY KEY,
    repo        TEXT NOT NULL,
    run_id      TEXT NOT NULL,
    artifact    TEXT NOT NULL,
    target      TEXT,
    action      TEXT NOT NULL,
    rationale   TEXT NOT NULL,
    evidence    TEXT NOT NULL,
    metric      TEXT NOT NULL,
    baseline    REAL,
    issue_url   TEXT,
    -- The issue this proposal became, as a number rather than only inside `issue_url`. It is
    -- the join back: the factory advances a proposal by noticing that the issue it filed got
    -- built and merged, and matching on a substring of a URL to do that would break the first
    -- time GitHub changed a path.
    issue_number INTEGER,
    pr_url      TEXT,
    status      TEXT NOT NULL DEFAULT 'proposed',
    observed    REAL,
    graded_at   TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS improvements_repo_idx ON improvements (repo);
CREATE INDEX IF NOT EXISTS improvements_status_idx ON improvements (status);
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
    # The commit the run branched from. Three of the things that decide how an agent behaves
    # — `.mem/`, `.factory.md`, and a repo's own skills — are files in the repo rather than
    # anything this plane sends, so without the base commit there is no way to say which
    # version of them a run actually had. That makes "did this change help?" unanswerable:
    # you can see that a rule was merged and that runs got better, and never establish that
    # the runs which got better were the ones carrying the rule. Nullable, because every run
    # recorded before this column existed genuinely has no answer.
    "ALTER TABLE runs ADD COLUMN base_sha TEXT",
    # The goal loop: a repo may carry an endstate description, and a run kind — 'plan',
    # an agent that compares the repo against it when the queue runs dry — files the next
    # issues or declares it met. No `runs` change: `kind` is a plain string column and 'plan'
    # is just another value of it. See the repos table comment in SCHEMA for what each of
    # these four holds.
    "ALTER TABLE repos ADD COLUMN goal TEXT",
    "ALTER TABLE repos ADD COLUMN goal_state TEXT NOT NULL DEFAULT 'none'",
    "ALTER TABLE repos ADD COLUMN plan_stalls INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE repos ADD COLUMN last_planned_at TEXT",
)

# Terminal states. Anything else means the run is still in flight.
TERMINAL = ("succeeded", "failed", "cancelled")

# Kinds that claim no issue, and so must not gate a repo's dispatch queue. Both are runs in
# every other sense — a VM, a streamed log, a cancel button — but neither is working on an
# issue, so treating them as "this repo is busy" stops the repo for the duration of something
# that was never in the way. `provision` learned this by stopping repos for a whole install;
# `learn` is here from the start for the same reason, and because a learning run reads a
# window of finished work that builds keep extending underneath it.
UNCLAIMED_KINDS = ("provision", "learn")


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


async def set_repo_goal(repo: str, goal: str | None, state: str, stalls: int = 0) -> None:
    """Record a repo's goal and reset the loop's counters to match it.

    One statement on purpose: a goal edit and the state it implies must land together, or a
    poll tick between the two writes reads a new goal with the old state — and either plans
    against a goal that was just declared met, or refuses to plan against one that was just
    written. `last_planned_at` is cleared so a fresh goal does not wait out the cooldown a
    previous one earned.
    """
    async with connect() as conn:
        await conn.execute(
            "UPDATE repos SET goal = ?, goal_state = ?, plan_stalls = ?, "
            "last_planned_at = NULL WHERE repo = ?",
            (goal, state, stalls, repo),
        )
        await conn.commit()


async def set_plan_state(
    repo: str,
    state: str | None = None,
    stalls: int | None = None,
    last_planned_at: str | None = None,
) -> None:
    """Record what the plan loop did, leaving the goal text alone.

    Explicit parameters rather than `**fields` for the same reason `CANDIDATE_COLUMNS` is a
    closed set: column names are interpolated into the statement, so the set of legal ones is
    decided here, not by the caller. `None` means "leave it as it is".
    """
    sets, values = [], []
    if state is not None:
        sets.append("goal_state = ?")
        values.append(state)
    if stalls is not None:
        sets.append("plan_stalls = ?")
        values.append(stalls)
    if last_planned_at is not None:
        sets.append("last_planned_at = ?")
        values.append(last_planned_at)
    if not sets:
        return
    async with connect() as conn:
        await conn.execute(
            f"UPDATE repos SET {', '.join(sets)} WHERE repo = ?", (*values, repo)
        )
        await conn.commit()


async def has_active_run(repo: str) -> bool:
    """True if `repo` already has a non-terminal run.

    This is the dispatch guard. The poller checks it before claiming, so a repo runs one
    issue at a time (lowest number first) and a slow label write can never cause a double
    dispatch — the database, not the issue label, decides what is already in flight.

    `UNCLAIMED_KINDS` are excluded. They are runs in every other sense — a VM, a streamed
    log, a cancel button — but they claim no issue, and counting them here made connecting a
    repo stop it: `POST /api/repos` starts a warm-up immediately, so a repo arriving with
    queued issues sat idle for the whole install. Nothing about a golden gates dispatch, and
    that is the point of the two-tier design: a repo with no warm snapshot boots `golden-copy`
    and installs for itself, including while its own snapshot is being built.
    """
    marks = ", ".join("?" for _ in TERMINAL)
    kind_marks = ", ".join("?" for _ in UNCLAIMED_KINDS)
    async with connect() as conn:
        async with conn.execute(
            f"SELECT 1 FROM runs WHERE repo = ? AND kind NOT IN ({kind_marks}) "
            f"AND status NOT IN ({marks}) LIMIT 1",
            (repo, *UNCLAIMED_KINDS, *TERMINAL),
        ) as cur:
            return await cur.fetchone() is not None


async def issues_since_last_learn(repo: str) -> int:
    """How many distinct issues this repo has finished since its last learning run.

    The trigger is volume rather than a clock because evidence, not time, is what a learning
    run consumes. A repo that shipped nothing this week has produced nothing new to read, and
    dispatching over the same window twice costs a VM and an agent to reach the same
    conclusions — or worse, different ones, from noise.

    Counts issues rather than runs so a single issue that took four retries counts once. Four
    dispatches at one problem is one piece of evidence about that problem, and counting them
    separately would make the flakiest repo learn most often on the least new information.

    A repo that has never had a learning run counts its whole history, which is what makes the
    first one fire as soon as there is anything worth reading.
    """
    async with connect() as conn:
        async with conn.execute(
            "SELECT MAX(created_at) AS last FROM runs WHERE repo = ? AND kind = 'learn'",
            (repo,),
        ) as cur:
            row = await cur.fetchone()
        since = (row["last"] if row else None) or ""
        async with conn.execute(
            "SELECT COUNT(DISTINCT issue_number) AS n FROM runs "
            "WHERE repo = ? AND kind = 'build' AND issue_number > 0 "
            "AND status IN ('succeeded', 'failed') AND created_at > ?",
            (repo, since),
        ) as cur:
            row = await cur.fetchone()
    return int(row["n"] if row else 0)


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


# --------------------------------------------------------------------------- improvements

# The life of a proposal, as edges rather than a status column anybody may set.
#
# `merged` is deliberately not terminal. A change that reached main is not finished, it is
# *live*, and the question it was created to answer — did the number move — cannot be asked
# until runs have happened under it. `kept` and `reverted` are the two answers, and they are
# the only reason this table earns its place: without a state after `merged`, the ledger would
# record what the loop did and never what it was worth, which is the failure mode that makes a
# self-improving system accumulate rules nobody dares delete.
IMPROVEMENT_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "proposed": ("building", "rejected", "abandoned"),
    "building": ("merged", "rejected"),
    "merged": ("kept", "reverted"),
}

IMPROVEMENT_INITIAL_STATUS = "proposed"

# What the loop is allowed to change. Two of these are here to be *recorded* rather than
# built, because what they change lives outside the fence the loop may open queued work
# against — a row exists so the insight is kept, read by a human, and seen by the next
# learning run so it stops rediscovering the same thing.
#
# `harness` is the dispatch flags, prompt templates and runner in this control plane.
#
# `compose` is the issue itself, and it is the highest-leverage of the lot. A builder can
# only be as good as what it was asked for: an ambiguous scope or an unverifiable acceptance
# criterion produces a rejection that looks exactly like an agent doing poor work, and every
# fix derived from that reading lands on the wrong thing — a skill teaching the builder to
# compensate for a badly-written issue, paid for on every future run, while the issues go on
# being written the same way. `FACTORY_MAX_REVIEW_CYCLES` already encodes this belief: two
# cycles, because a third failure means the issue is wrong rather than the code.
IMPROVEMENT_ARTIFACTS = frozenset({
    "skill", "factory_md", "mem", "candidate", "harness", "compose",
})

# Artifacts whose fix is not in the repo being learned about, so a proposal about one is
# filed for a human and never labelled `agent:queued` — queuing it would point the factory at
# its own control plane, or at the skill that writes its work orders.
IMPROVEMENT_UNBUILDABLE = frozenset({"harness", "compose"})

# `revert` is an action rather than a status because it is a change like any other: it needs
# its own issue, its own review, and its own row saying what it undid and why.
IMPROVEMENT_ACTIONS = frozenset({"add", "edit", "delete", "revert"})

# Closed for the same reason `CANDIDATE_COLUMNS` is: these values arrive from a file an agent
# wrote inside a VM, and column *names* are interpolated into the statement because SQL has no
# placeholder for an identifier.
IMPROVEMENT_COLUMNS = frozenset({
    "id", "repo", "run_id", "artifact", "target", "action", "rationale", "evidence",
    "metric", "baseline", "issue_url", "issue_number", "pr_url", "status", "observed",
    "graded_at", "created_at", "updated_at",
})

# Every proposal has to say what it is for and what would show it worked. Enforced here rather
# than trusted to the prompt, because these are exactly the fields a model under pressure to
# produce three proposals will leave blank, and a proposal with no metric can never be graded —
# it would enter the ledger already immune to deletion.
IMPROVEMENT_REQUIRED = ("repo", "run_id", "artifact", "action", "rationale", "evidence", "metric")


async def create_improvement(**fields: Any) -> None:
    """Record a proposed change. Re-recording the same id leaves the original alone.

    Raises `ValueError` on an unknown column, a missing justification field, an artifact or
    action outside the closed sets, or any status other than `proposed` — a proposal that
    could name its own status could arrive `merged` having never been built.
    """
    fields = {"status": IMPROVEMENT_INITIAL_STATUS, **fields}
    unknown = sorted(set(fields) - IMPROVEMENT_COLUMNS)
    if unknown:
        raise ValueError(f"unknown improvements column(s): {', '.join(unknown)}")
    if fields["status"] != IMPROVEMENT_INITIAL_STATUS:
        raise ValueError(
            f"an improvement is created {IMPROVEMENT_INITIAL_STATUS}, not {fields['status']!r}; "
            f"use transition_improvement"
        )
    missing = [k for k in IMPROVEMENT_REQUIRED if not str(fields.get(k) or "").strip()]
    if missing:
        raise ValueError(f"improvement is missing: {', '.join(missing)}")
    if fields["artifact"] not in IMPROVEMENT_ARTIFACTS:
        raise ValueError(
            f"unknown artifact {fields['artifact']!r}; "
            f"expected one of {', '.join(sorted(IMPROVEMENT_ARTIFACTS))}"
        )
    if fields["action"] not in IMPROVEMENT_ACTIONS:
        raise ValueError(
            f"unknown action {fields['action']!r}; "
            f"expected one of {', '.join(sorted(IMPROVEMENT_ACTIONS))}"
        )
    now = utcnow()
    fields.setdefault("created_at", now)
    fields.setdefault("updated_at", now)
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    async with connect() as conn:
        await conn.execute(
            f"INSERT OR IGNORE INTO improvements ({cols}) VALUES ({marks})",
            tuple(fields.values()),
        )
        await conn.commit()


async def get_improvement(improvement_id: str) -> dict | None:
    async with connect() as conn:
        async with conn.execute(
            "SELECT * FROM improvements WHERE id = ?", (improvement_id,)
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def list_improvements(
    repo: str | None = None, status: str | None = None, limit: int = 200
) -> list[dict]:
    """The ledger, newest first, optionally scoped.

    Newest first because both readers want the recent end: a human is auditing what just
    happened, and a learning run needs what it most recently tried before it proposes again.

    `rowid` breaks ties, and it is not decoration. `utcnow()` has second granularity and a
    learning run files its proposals in a loop, so a batch shares a timestamp — ordering on
    `created_at` alone leaves them in whatever order the query planner felt like, and "the
    most recent thing this loop tried" becomes a different answer each time it is asked.
    """
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
            f"SELECT * FROM improvements{where} ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (*params, limit),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def improvement_for_issue(repo: str, issue_number: int) -> dict | None:
    """The proposal that became this issue, if it was one.

    Most issues are not: the factory is mostly doing work a human asked for, and this returns
    None for all of it. It exists so the few that *are* the loop's own output can be followed
    from "filed" through to "merged", which is the only way a change ever becomes gradeable.
    """
    if not issue_number:
        return None
    async with connect() as conn:
        async with conn.execute(
            "SELECT * FROM improvements WHERE repo = ? AND issue_number = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (repo, issue_number),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def transition_improvement(
    improvement_id: str,
    to_status: str,
    observed: float | None = None,
    **fields: Any,
) -> dict:
    """Move a proposal along an allowed edge, optionally recording what it turned out to be worth.

    `observed` is accepted here rather than in a separate write because grading *is* the
    transition: `merged` becomes `kept` or `reverted` precisely by measuring, and letting the
    number be written without moving the status would allow a graded row that still claims to
    be ungraded.

    Extra `fields` carry the facts that only exist once the change is under way — `issue_url`
    when it is picked up, `pr_url` when one opens. Same closed column set as creation.
    """
    unknown = sorted(set(fields) - IMPROVEMENT_COLUMNS)
    if unknown:
        raise ValueError(f"unknown improvements column(s): {', '.join(unknown)}")
    improvement = await get_improvement(improvement_id)
    if improvement is None:
        raise ValueError(f"no such improvement: {improvement_id}")
    current = improvement["status"]
    allowed = IMPROVEMENT_TRANSITIONS.get(current, ())
    if to_status not in allowed:
        raise ValueError(
            f"cannot transition improvement {improvement_id} from {current} to {to_status}"
        )
    now = utcnow()
    updates: dict[str, Any] = {"status": to_status, "updated_at": now, **fields}
    if observed is not None:
        updates["observed"] = observed
        updates["graded_at"] = now
    sets = ", ".join(f"{k} = ?" for k in updates)
    async with connect() as conn:
        # Guarded on the status read above, for the reason `transition_candidate` gives: two
        # writers both seeing `merged` must not both succeed with the slower silently winning.
        cursor = await conn.execute(
            f"UPDATE improvements SET {sets} WHERE id = ? AND status = ?",
            (*updates.values(), improvement_id, current),
        )
        await conn.commit()
        if not cursor.rowcount:
            raise ValueError(
                f"cannot transition improvement {improvement_id} from {current} to "
                f"{to_status}: it changed underneath this request"
            )
    improvement.update(updates)
    return improvement
