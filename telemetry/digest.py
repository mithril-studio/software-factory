"""What went wrong lately, as evidence rather than prose.

The trace layer has been able to answer "how did the last fifty runs go" since it shipped.
Nothing asked. `backlog.md` names the tell about the one time somebody did: *"The 2026-08-12
analysis is repeatable. It was excellent and manual."* This module is that analysis as a
query, so the answer exists whether or not a human thought to look for it.

It is deliberately passive — SQL over the shared database, no model, no network, consistent
with the passive-layer principle in `docs/architecture.md` §1. It reports; it concludes
nothing. The reader that turns this into a proposal is an agent inside a VM, which is the
only thing in this system allowed to have an opinion.

**What this deliberately does not do.** It never reads a repo's `.mem/`. The highest-value
question here — *is this recurring failure one we already wrote a memory record about?* —
needs record titles, and record bodies live in the repo, not in this database. Fetching them
would mean `telemetry` reaching for GitHub, or importing `control`, and the dependency runs
one way (`docs/architecture.md` §3.2). It is also unnecessary: the agent that reads this
digest has the repo checked out, so it can open `.mem/` itself. This layer supplies the half
that only it can see — *which* runs failed, how often, and what they retrieved — and the
correlation happens where both halves are already in hand.

That correlation is worth stating plainly because it is the point of joining the two stores
at all: a failure that keeps happening **while a memory record already documents it** is a
retrieval failure, not a knowledge failure, and the fix is a different fix — usually the
record's `files` list, which is the only thing priming matches against.

Ordered by the objective the loop optimises: review rejections and runs that shipped nothing
come first, cost comes last. Cost is reported, not optimised.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from .store import COST_SQL, PRICE_JOIN, _rows

# How much of each section survives into the output. A digest is read by something with a
# context window, so it has a size whether or not anybody chooses one — the choice is between
# picking the limit here and having a prompt truncate it somewhere arbitrary.
#
# Every cap reports what it dropped (see `truncated` in the result). A limit that silently
# discards rows reads downstream as "this is everything", and an agent told to propose from a
# complete picture will do exactly that with a partial one.
SECTION_LIMIT = 20
EVIDENCE_RUNS = 5
REASON_MAX = 300

# Runs the digest never counts. A learning run's own cost, tools and failures are not evidence
# about how the factory builds software, and feeding them back in would let the loop react to
# itself — the cheapest way to make a self-improving system oscillate.
EXCLUDED_KINDS = ("learn",)

# Volatile fragments of an error string, removed before two errors are called the same thing.
# Without this every failure is unique — run ids, shas, ports, durations and tmp paths differ
# on every run, so a message that recurred forty times clusters as forty singletons and the
# most common failure in the fleet is invisible underneath its own detail.
_NOISE = (
    (re.compile(r"\b[0-9a-f]{7,64}\b", re.I), "<sha>"),      # run ids, commit shas
    (re.compile(r"\b\d+(\.\d+)?(ms|s|m|h)\b", re.I), "<dur>"),  # 90m, 1.5s
    (re.compile(r"/tmp/[^\s'\"]+"), "<tmp>"),
    (re.compile(r"\b\d+\b"), "<n>"),
    (re.compile(r"\s+"), " "),
)


def signature(error: str | None) -> str | None:
    """A stable key for "this is the same failure as that one".

    Lossy on purpose and only ever used for grouping — the full text of representative
    errors is carried alongside the cluster, so nothing here is the only copy of anything.
    """
    if not error or not error.strip():
        return None
    text = error.strip().lower()
    for pattern, replacement in _NOISE:
        text = pattern.sub(replacement, text)
    return text.strip()[:REASON_MAX] or None


def _window(repo: str | None, days: int) -> tuple[str, list[Any]]:
    """The predicate every section shares, and the reason they all join `runs`.

    Windowing on `runs.created_at` rather than each table's own timestamp is not a
    convenience. `created_at` is written by `db.utcnow()` in one format; the `ts` on a tool
    call is whatever the agent runtime emitted, and the two are compared as strings. Mixing
    them would make the boundary of the window depend on which table a row came from. It is
    also the more truthful grouping: a run belongs to the window by when it started, and
    everything it did belongs with it, including the part that ran past midnight.
    """
    since = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    ).isoformat(timespec="seconds")
    marks = ", ".join("?" for _ in EXCLUDED_KINDS)
    sql = f"r.created_at >= ? AND COALESCE(r.kind, 'build') NOT IN ({marks})"
    params: list[Any] = [since, *EXCLUDED_KINDS]
    if repo:
        sql += " AND r.repo = ?"
        params.append(repo)
    return sql, params


def _cap(rows: list[dict], limit: int = SECTION_LIMIT) -> tuple[list[dict], int]:
    """`limit` rows and how many were dropped. Callers must report the second number."""
    return rows[:limit], max(0, len(rows) - limit)


async def _outcomes(where: str, params: list[Any]) -> list[dict]:
    """Per repo: did the work ship, and what did it take to get there.

    `shipped` counts issues that reached a pull request, not runs that exited zero — the
    factory's output is merged work, and a build that succeeded into nothing is the failure
    mode this whole loop exists to notice. Retries and fix cycles are counted separately
    because they answer to separate budgets (`docs/architecture.md` §4).
    """
    return await _rows(
        f"""
        SELECT r.repo,
               COUNT(*)                                                AS runs,
               COUNT(DISTINCT r.issue_number)                          AS issues,
               COUNT(DISTINCT CASE WHEN r.pr_url IS NOT NULL
                                   THEN r.issue_number END)            AS shipped,
               SUM(CASE WHEN r.status = 'failed' THEN 1 ELSE 0 END)    AS failed_runs,
               SUM(CASE WHEN r.status = 'succeeded' AND r.pr_url IS NULL
                        AND r.kind = 'build' THEN 1 ELSE 0 END)        AS shipped_nothing,
               SUM(CASE WHEN COALESCE(r.attempt, 1) > 1 THEN 1 ELSE 0 END) AS retries,
               MAX(COALESCE(r.cycle, 1))                               AS max_cycle,
               COUNT(DISTINCT r.base_sha)                              AS context_versions
        FROM runs r
        WHERE {where}
        GROUP BY r.repo ORDER BY failed_runs DESC, r.repo
        """,
        tuple(params),
    )


async def _rejections(where: str, params: list[Any]) -> list[dict]:
    """Why reviews sent work back, clustered.

    A rejection is the highest-signal row in the database: a labelled example of the agent
    producing something that did not pass, with a reason attached by something that looked.
    """
    rows = await _rows(
        f"""
        SELECT r.repo, r.issue_number, r.error, r.verdict, r.created_at, r.id AS run_id
        FROM runs r
        WHERE {where} AND r.kind = 'review' AND r.error IS NOT NULL AND r.error != ''
        ORDER BY r.created_at DESC
        """,
        tuple(params),
    )
    return _cluster(rows)


async def _failures(where: str, params: list[Any]) -> list[dict]:
    """Runs that ended badly, clustered by what they said on the way out."""
    rows = await _rows(
        f"""
        SELECT r.repo, r.issue_number, r.error, r.created_at, r.id AS run_id
        FROM runs r
        WHERE {where} AND r.status = 'failed' AND r.error IS NOT NULL AND r.error != ''
        ORDER BY r.created_at DESC
        """,
        tuple(params),
    )
    return _cluster(rows)


def _cluster(rows: list[dict]) -> list[dict]:
    """Group rows sharing an error signature, newest first, keeping evidence.

    Each cluster carries the run ids behind it. That is not decoration: a proposal built on
    this digest is required to cite runs, and a cluster that could not name its own would
    make every proposal derived from it uncitable.
    """
    groups: dict[str, dict] = {}
    for row in rows:
        key = signature(row.get("error"))
        if key is None:
            continue
        group = groups.setdefault(key, {
            "signature": key,
            "count": 0,
            "repos": set(),
            "issues": set(),
            "example": (row.get("error") or "")[:REASON_MAX],
            "run_ids": [],
            "last_seen": row.get("created_at"),
        })
        group["count"] += 1
        if row.get("repo"):
            group["repos"].add(row["repo"])
        if row.get("issue_number"):
            group["issues"].add(row["issue_number"])
        if len(group["run_ids"]) < EVIDENCE_RUNS:
            group["run_ids"].append(row.get("run_id"))
    out = []
    for group in groups.values():
        group["repos"] = sorted(group["repos"])
        group["issues"] = sorted(group["issues"])
        out.append(group)
    return sorted(out, key=lambda g: (-g["count"], g["signature"]))


async def _tool_errors(where: str, params: list[Any]) -> list[dict]:
    """Which tools failed, and on what. The raw event evidence under a run's summary."""
    return await _rows(
        f"""
        SELECT tc.tool,
               COUNT(*)                  AS failures,
               COUNT(DISTINCT tc.run_id) AS runs,
               MIN(tc.error)             AS example
        FROM tool_calls tc
        JOIN runs r ON r.id = tc.run_id
        WHERE {where} AND tc.ok = 0
        GROUP BY tc.tool ORDER BY failures DESC
        """,
        tuple(params),
    )


async def _retrieval(where: str, params: list[Any]) -> list[dict]:
    """What memory did for runs that failed, versus runs that did not.

    The comparison is the useful part. "Runs primed memory" says nothing on its own; "the
    runs that failed opened a third as many records as the ones that shipped" is a lead. It
    is still only a lead — this cannot see whether an opened record was relevant, and a run
    that opened nothing may simply not have needed anything.
    """
    return await _rows(
        f"""
        SELECT r.repo,
               CASE WHEN r.status = 'failed' OR (r.kind = 'build' AND r.pr_url IS NULL)
                    THEN 'went_wrong' ELSE 'went_fine' END       AS outcome,
               COUNT(DISTINCT r.id)                              AS runs,
               COUNT(DISTINCT mrc.run_id)                        AS primed,
               COUNT(mr.memory_id)                               AS records_opened,
               COALESCE(AVG(mrc.indexed), 0)                     AS avg_index_size
        FROM runs r
        LEFT JOIN memory_receipts mrc ON mrc.run_id = r.id
        LEFT JOIN memory_reads mr     ON mr.run_id  = r.id
        WHERE {where}
        GROUP BY r.repo, outcome ORDER BY r.repo, outcome
        """,
        tuple(params),
    )


async def _skills(where: str, params: list[Any]) -> list[dict]:
    """Repo skills the window's runs actually loaded.

    Windowed by run rather than by the tool call's own timestamp, for the reason `_window`
    gives. The absence of a skill from this list is the eviction signal — but only the agent
    can use it, because only the agent can see which skills the repo *has*, and a skill
    missing from both lists is simply a skill nobody wrote.
    """
    from .normalize import SKILL_TOOL

    return await _rows(
        f"""
        SELECT r.repo,
               tc.detail                 AS skill,
               COUNT(*)                  AS loads,
               COUNT(DISTINCT tc.run_id) AS runs,
               MAX(tc.ts)                AS last_loaded
        FROM tool_calls tc
        JOIN runs r ON r.id = tc.run_id
        WHERE {where} AND tc.tool = ? AND tc.detail IS NOT NULL AND tc.detail != ''
        GROUP BY r.repo, tc.detail ORDER BY r.repo, loads DESC
        """,
        (*params, SKILL_TOOL),
    )


async def _candidates(repo: str | None) -> list[dict]:
    """Learnings agents proposed and nobody has decided on.

    A candidate is a paragraph an agent wrote when it noticed something but was not confident
    enough to commit it (`docs/architecture.md` §3.3). They accumulate: accepting one records a
    verdict and deliberately does not write `.mem/`, so a queue of accepted-but-unwritten
    learnings is the normal state, and the thing that eventually writes them is a later agent.

    Included here because a learning run is that agent, and because the queue is evidence in
    its own right — a candidate filed four times by four different runs is a repo telling you
    something about itself more clearly than any single failure does.

    Not windowed. A candidate has no outcome and does not go stale the way a run does; one
    filed two months ago and never decided is *more* worth surfacing, not less.
    """
    where = "WHERE status = 'pending'"
    params: list[Any] = []
    if repo:
        where += " AND repo = ?"
        params.append(repo)
    return await _rows(
        f"""
        SELECT repo, domain, type, title, COUNT(*) AS filed, MAX(created_at) AS last_filed
        FROM memory_candidates
        {where}
        GROUP BY repo, domain, type, title
        ORDER BY filed DESC, last_filed DESC
        """,
        tuple(params),
    )


async def _cost(where: str, params: list[Any]) -> list[dict]:
    """Spend in the window, and how much of it bought nothing. Lowest-priority section."""
    return await _rows(
        f"""
        WITH run_cost AS (
            SELECT c.run_id, SUM({COST_SQL}) AS cost
            {PRICE_JOIN}
            GROUP BY c.run_id
        )
        SELECT r.repo,
               COALESCE(SUM(rc.cost), 0)                      AS spend,
               COALESCE(SUM(CASE WHEN r.pr_url IS NULL
                                 THEN rc.cost ELSE 0 END), 0) AS wasted,
               COUNT(DISTINCT CASE WHEN r.pr_url IS NOT NULL
                                   THEN r.issue_number END)   AS shipped
        FROM runs r
        LEFT JOIN run_cost rc ON rc.run_id = r.id
        WHERE {where}
        GROUP BY r.repo ORDER BY spend DESC
        """,
        tuple(params),
    )


async def build(repo: str | None = None, days: int = 14) -> dict:
    """The window's evidence, capped and ordered by what the loop optimises.

    Returns a plain dict, JSON-serialisable, safe to hand to a prompt. `truncated` names
    every section a cap shortened and by how much, so a reader can tell "nothing else
    happened" from "we stopped listing".
    """
    where, params = _window(repo, days)

    outcomes = await _outcomes(where, params)
    rejections, rejections_dropped = _cap(await _rejections(where, params))
    failures, failures_dropped = _cap(await _failures(where, params))
    tool_errors, tool_dropped = _cap(await _tool_errors(where, params))
    skills, skills_dropped = _cap(await _skills(where, params))
    candidates, candidates_dropped = _cap(await _candidates(repo))

    return {
        "window": {"days": days, "since": params[0], "repo": repo},
        # Ordered as the objective is: what got rejected, what shipped nothing, what broke,
        # what memory did about it, which skills were read, and only then what it cost.
        "rejections": rejections,
        "outcomes": outcomes,
        "failures": failures,
        "tool_errors": tool_errors,
        "retrieval": await _retrieval(where, params),
        "candidates": candidates,
        "skills": skills,
        "cost": await _cost(where, params),
        "truncated": {
            k: v for k, v in (
                ("rejections", rejections_dropped),
                ("failures", failures_dropped),
                ("tool_errors", tool_dropped),
                ("skills", skills_dropped),
                ("candidates", candidates_dropped),
            ) if v
        },
    }
