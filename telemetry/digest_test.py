"""The digest has to be honest about two things it would be easy to fake.

First, that it is not reading its own output. A learning run is a run: it has a cost, tools,
and a way of failing. Counted, it would make the loop react to its own behaviour, which is
the cheapest way to make a self-improving system oscillate — and the failure would look like
insight, because the numbers would keep moving.

Second, that a cap says so. Every section is bounded, because something with a context window
reads this. A bound that drops rows quietly reads downstream as "this is everything", and an
agent asked to propose from a complete picture will do exactly that with a partial one.

The clustering is the third thing, and it is the one that decides whether any of this is
usable: run ids and durations differ on every run, so without normalisation the failure that
happened forty times arrives as forty singletons and is invisible underneath its own detail.

Run it directly, no framework needed:

    .venv/bin/python -m telemetry.digest_test
"""
import asyncio
import datetime as dt
import sys
import tempfile
from pathlib import Path

import aiosqlite

from telemetry import config, digest, normalize, store

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


def ago(days: int) -> str:
    """Timestamps in `db.utcnow()`'s format, which is what the window compares against."""
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat(timespec="seconds")


async def seed_runs(rows):
    """`runs` belongs to `control`; this layer reads it and never writes it. Built by hand
    here rather than imported, which is the same boundary `store_test` keeps."""
    async with aiosqlite.connect(config.db_path) as conn:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS runs ("
            " id TEXT PRIMARY KEY, repo TEXT, issue_number INTEGER, status TEXT,"
            " kind TEXT, verdict TEXT, error TEXT, pr_url TEXT, attempt INTEGER,"
            " cycle INTEGER, base_sha TEXT, created_at TEXT)"
        )
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS memory_candidates ("
            " id TEXT PRIMARY KEY, run_id TEXT, repo TEXT, domain TEXT, type TEXT,"
            " title TEXT, body TEXT, evidence TEXT, confidence TEXT, status TEXT,"
            " created_at TEXT, updated_at TEXT)"
        )
        await conn.executemany(
            "INSERT OR REPLACE INTO runs (id, repo, issue_number, status, kind, error,"
            " pr_url, attempt, cycle, base_sha, created_at)"
            " VALUES (:id, :repo, :issue, :status, :kind, :error, :pr, :attempt, :cycle,"
            " :base_sha, :created_at)",
            rows,
        )
        await conn.commit()


async def seed_candidates(rows):
    async with aiosqlite.connect(config.db_path) as conn:
        await conn.executemany(
            "INSERT OR REPLACE INTO memory_candidates"
            " (id, run_id, repo, domain, type, title, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        await conn.commit()


def r(id, repo="acme/web", issue=1, status="succeeded", kind="build", error=None,
      pr=None, attempt=1, cycle=1, base_sha="sha1", days=1):
    return dict(id=id, repo=repo, issue=issue, status=status, kind=kind, error=error,
                pr=pr, attempt=attempt, cycle=cycle, base_sha=base_sha, created_at=ago(days))


run(seed_runs([
    # Two builds that shipped, one that succeeded into nothing.
    r("b1", issue=1, pr="https://pr/1"),
    r("b2", issue=2, pr="https://pr/2"),
    r("b3", issue=3, pr=None),
    # The same crash twice, differing only in the volatile parts.
    r("b4", issue=4, status="failed", attempt=2,
      error="crashed: agent exited after 90m waiting on run 4f3a9c2b1d8e7a60"),
    r("b5", issue=5, status="failed", attempt=2,
      error="crashed: agent exited after 45m waiting on run 9c1b7e4a2f6d3b85"),
    # Two reviews that sent work back.
    r("v1", issue=1, kind="review", error="not merged: criterion api-429 not_met"),
    r("v2", issue=2, kind="review", error="ci red: typecheck"),
    # Excluded: a learning run that failed expensively. If it is counted, the loop is
    # reading its own behaviour back as evidence about how the factory builds software.
    r("l1", issue=0, kind="learn", status="failed",
      error="crashed: the learning run itself fell over"),
    # Excluded: outside the window.
    r("old", issue=9, status="failed", days=90, error="ancient history"),
    # A second repo, so scoping has something to get wrong.
    r("o1", repo="acme/api", issue=7, status="failed", base_sha="sha9",
      error="crashed: agent exited after 12m waiting on run aaaabbbbccccdddd"),
]))

run(store.write([
    normalize.ToolCall(id="t1", run_id="b4", turn=1, ts=ago(1), tool="Bash", ok=False,
                       duration_ms=10, error="npm ERR! missing script: verify", detail="npm run verify"),
    normalize.ToolCall(id="t2", run_id="b5", turn=1, ts=ago(1), tool="Bash", ok=False,
                       duration_ms=10, error="npm ERR! missing script: verify", detail="npm run verify"),
    normalize.ToolCall(id="t3", run_id="b1", turn=1, ts=ago(1), tool=normalize.SKILL_TOOL,
                       ok=True, duration_ms=5, error=None, detail="verify-before-pr"),
    # A skill loaded only by the excluded learning run. It must not appear: a skill the loop
    # itself uses is not evidence that the repo's builds need it.
    normalize.ToolCall(id="t4", run_id="l1", turn=1, ts=ago(1), tool=normalize.SKILL_TOOL,
                       ok=True, duration_ms=5, error=None, detail="digest-reading"),
]))

run(seed_candidates([
    # Two runs noticed the same thing independently. That repetition is the signal.
    ("c1", "b1", "acme/web", "repository", "failure", "verify.sh is the only gate list",
     "pending", ago(9), ago(9)),
    ("c2", "b2", "acme/web", "repository", "failure", "verify.sh is the only gate list",
     "pending", ago(3), ago(3)),
    ("c3", "b3", "acme/web", "repository", "convention", "tests are plain __main__ modules",
     "pending", ago(2), ago(2)),
    # Already decided, so no longer waiting on anything.
    ("c4", "b1", "acme/web", "repository", "pattern", "already triaged", "accepted",
     ago(4), ago(4)),
    ("c5", "o1", "acme/api", "repository", "failure", "another repo's candidate", "pending",
     ago(1), ago(1)),
]))

run(store.write_memory_receipt("b1", 40, ["repository"], ago(1)))
run(store.write_memory_reads("b1", [("mem_a", ago(1)), ("mem_b", ago(1))]))
run(store.write_memory_receipt("b4", 40, ["repository"], ago(1)))


d = run(digest.build(days=30))


# ---------- AC1: the loop does not read its own output

sigs = " ".join(f["signature"] for f in d["failures"])
check("the learning run's own crash is not evidence", "learning run" in sigs, False)
check("nor is anything outside the window", "ancient history" in sigs, False)
skills = [s["skill"] for s in d["skills"]]
check("a skill only the learning run loaded is not counted", "digest-reading" in skills, False)
check("a skill a build loaded is", "verify-before-pr" in skills, True)

web = next(o for o in d["outcomes"] if o["repo"] == "acme/web")
check("the excluded kinds are not in the run tally", web["runs"], 7)


# ---------- AC2: clustering survives the volatile parts

# Two crashes differing only by duration and run id are one failure, not two. This is the
# whole reason the section is readable: without it the most common failure in the fleet is
# spread across as many rows as it has instances.
crash = [f for f in d["failures"] if "exited after" in f["signature"]]
check("the same crash clusters into one row", len(crash), 1)
check("and counts every instance, across repos", crash[0]["count"], 3)
check("naming the repos it hit", crash[0]["repos"], ["acme/api", "acme/web"])

# Evidence, not decoration: a proposal built on this digest is required to cite runs, so a
# cluster that could not name its own would make everything derived from it uncitable.
check("the cluster carries run ids as evidence", sorted(crash[0]["run_ids"]),
      ["b4", "b5", "o1"])
check("and keeps one error verbatim", "crashed: agent exited" in crash[0]["example"], True)

check("volatile ids and durations are normalised away",
      digest.signature("crashed after 90m on run 4f3a9c2b1d8e7a60")
      == digest.signature("crashed after 45m on run 9c1b7e4a2f6d3b85"), True)
# But not so aggressively that different failures merge. Over-normalising would be the same
# defect from the other side: one enormous cluster that means nothing.
check("genuinely different failures stay apart",
      digest.signature("ci red: typecheck") == digest.signature("ci red: lint"), False)
check("an empty error has no signature", digest.signature("  "), None)


# ---------- AC3: rejections are their own section, ordered first

check("reviews are clustered separately from crashes", len(d["rejections"]), 2)
check("rejections come before cost in the payload",
      list(d).index("rejections") < list(d).index("cost"), True)


# ---------- AC4: caps report what they dropped

check("nothing was truncated at this size", d["truncated"], {})

already = len(d["failures"])
added = digest.SECTION_LIMIT + 4
many = [r(f"m{i}", issue=100 + i, status="failed", error=f"distinct failure {chr(65 + i)}")
        for i in range(added)]
run(seed_runs(many))
big = run(digest.build(days=30))
check("the section is capped", len(big["failures"]), digest.SECTION_LIMIT)
check("and says how many it dropped", big["truncated"].get("failures"),
      already + added - digest.SECTION_LIMIT)
# The point of the previous two lines together: a reader can tell "nothing else happened"
# from "we stopped listing", which a bare cap makes indistinguishable.
check("sections that fit are absent from truncated", "rejections" in big["truncated"], False)


# ---------- AC5: scoping, and the outcome split memory is measured against

one = run(digest.build(repo="acme/api", days=30))
check("a repo-scoped digest sees only that repo",
      sorted({o["repo"] for o in one["outcomes"]}), ["acme/api"])

buckets = {(x["repo"], x["outcome"]): x for x in d["retrieval"]}
check("runs that went wrong are separated from runs that did not",
      ("acme/web", "went_wrong") in buckets and ("acme/web", "went_fine") in buckets, True)
check("a build that succeeded into no PR counts as having gone wrong",
      buckets[("acme/web", "went_wrong")]["runs"] >= 3, True)
check("records opened are attributed to the runs that opened them",
      buckets[("acme/web", "went_fine")]["records_opened"], 2)

check("tool failures are grouped by tool",
      [(t["tool"], t["failures"], t["runs"]) for t in d["tool_errors"]], [("Bash", 2, 2)])


# ---------- AC6: the candidate queue, which is evidence somebody already wrote down

cands = {c["title"]: c for c in d["candidates"]}
# The repetition is the point: two runs filing the same learning independently is the repo
# stating something about itself more plainly than any single failure does.
check("a candidate filed twice is reported with its count",
      cands["verify.sh is the only gate list"]["filed"], 2)
check("a candidate filed once is still reported",
      cands["tests are plain __main__ modules"]["filed"], 1)
check("repeated candidates sort first", d["candidates"][0]["filed"], 2)
check("already-triaged candidates are not still waiting", "already triaged" in cands, False)

# An unscoped digest is fleet-wide and reports every repo's queue; a scoped one must not.
check("an unscoped digest reports both repos' candidates",
      {c["repo"] for c in d["candidates"]}, {"acme/web", "acme/api"})
check("a scoped digest reports only its own",
      {c["repo"] for c in one["candidates"]}, {"acme/api"})

# Deliberately not windowed. A candidate has no outcome and does not go stale the way a run
# does — one filed months ago and never decided is more worth surfacing, not less. `c1` was
# filed nine days ago and must survive a one-day window.
narrow = run(digest.build(repo="acme/web", days=1))
check("a candidate older than the window is still surfaced",
      cands["verify.sh is the only gate list"]["last_filed"] < narrow["window"]["since"], True)
check("and still carries its full count, not the part inside the window",
      {c["title"]: c["filed"] for c in narrow["candidates"]}["verify.sh is the only gate list"], 2)

# Attribution: the column that makes "did that change help?" answerable at all.
check("the window reports how many context versions it spans", web["context_versions"], 1)


print()
if fails:
    print(f"{len(fails)} failed: {', '.join(fails)}")
    sys.exit(1)
print("all passed")
