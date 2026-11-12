"""The runs table: claiming a repo, finishing a run once, and opening an old database.

Two of these guard races that used to be check-then-act across an `await`: two dispatches for
one repo, and a cancel overwriting a run that finished in the same moment. The last section
opens a database written by the previous version of the control plane, because v1 must start
on the production database as it is.

Touches SQLite, against throwaway files. Run it directly, no framework needed:

    .venv/bin/python -m control.db_test
"""
import asyncio
import sqlite3
import sys
import tempfile
from pathlib import Path

from control import db
from control.config import settings

fails = []


def check(name, got, want=True):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n       got={got!r} want={want!r}"))
    if not ok:
        fails.append(name)


tmp = tempfile.TemporaryDirectory()
object.__setattr__(settings, "db_path", Path(tmp.name) / "factory.db")


def run(coro):
    return asyncio.run(coro)


run(db.init())
run(db.init())
check("init: a second init is a no-op, not an error", True)


def row(run_id, repo="acme/api", status="queued"):
    return dict(id=run_id, repo=repo, issue_number=1, status=status, created_at=db.utcnow())


# ---------- claim
check("claim: an idle repo is claimed", run(db.claim_run(**row("r1"))), True)
check("claim: a second claim on the same repo is refused", run(db.claim_run(**row("r2"))), False)
check("claim: and leaves no row behind", run(db.get_run("r2")), None)
check("claim: another repo is independent", run(db.claim_run(**row("r3", repo="acme/web"))), True)


async def race():
    return await asyncio.gather(*(db.claim_run(**row(f"race{i}", repo="acme/race")) for i in range(8)))


check("claim: eight concurrent claims for one repo, exactly one wins", sum(run(race())), 1)

# ---------- finish
check("finish: a live run finishes", run(db.finish_run("r1", "succeeded", pr_url="u")), True)
check("finish: a finished run cannot be finished again", run(db.finish_run("r1", "cancelled")), False)
r1 = run(db.get_run("r1"))
check("finish: the first outcome stands", (r1["status"], r1["pr_url"]), ("succeeded", "u"))
check("finish: and is stamped", bool(r1["finished_at"]))
check("claim: a repo whose run finished can be claimed again", run(db.claim_run(**row("r4"))), True)
active = {r["id"] for r in run(db.active_runs())}
check("active: lists unfinished runs and not finished ones",
      ("r3" in active, "r4" in active, "r1" in active), (True, True, False))
check("active: has_active_run agrees", run(db.has_active_run("acme/api")), True)
run(db.finish_run("r4", "failed", error="x"))
check("active: and goes false when the run ends", run(db.has_active_run("acme/api")), False)

stats = run(db.stats_by_repo())
check("stats: tallies per repo", (stats["acme/api"]["succeeded"], stats["acme/api"]["failed"]), (1, 1))

# ---------- an old database
# The schema the previous control plane wrote: more columns, NOT NULL defaults on the repos
# table, no over_budget. v1 must open it, add what it needs, and write rows into it.
OLD = """
CREATE TABLE runs (
    id TEXT PRIMARY KEY, repo TEXT NOT NULL, issue_number INTEGER NOT NULL, issue_title TEXT,
    branch TEXT, golden TEXT, vm_name TEXT, vm_id TEXT, status TEXT NOT NULL, exit_code INTEGER,
    pr_url TEXT, error TEXT, log_path TEXT, transcript_path TEXT,
    attempt INTEGER NOT NULL DEFAULT 1, cycle INTEGER NOT NULL DEFAULT 1,
    kind TEXT NOT NULL DEFAULT 'build', verdict TEXT, manifest TEXT, agent TEXT,
    tokens_in INTEGER, tokens_out INTEGER, cost_usd REAL, merged_sha TEXT, release_id INTEGER,
    base_sha TEXT, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT
);
CREATE TABLE repos (
    repo TEXT PRIMARY KEY, added_at TEXT NOT NULL, golden TEXT,
    provision_status TEXT NOT NULL DEFAULT 'none', agent TEXT, goal_sha TEXT,
    goal_state TEXT NOT NULL DEFAULT 'none', plan_stalls INTEGER NOT NULL DEFAULT 0,
    last_planned_at TEXT, base_branch TEXT
);
CREATE TABLE releases (id INTEGER PRIMARY KEY, repo TEXT NOT NULL, status TEXT NOT NULL);
INSERT INTO runs (id, repo, issue_number, status, kind, created_at)
    VALUES ('old1', 'acme/api', 3, 'succeeded', 'review', '2026-08-01T00:00:00+00:00');
INSERT INTO repos (repo, added_at, golden) VALUES ('acme/api', '2026-08-01T00:00:00+00:00', 'g');
"""
old = Path(tmp.name) / "old.db"
with sqlite3.connect(old) as conn:
    conn.executescript(OLD)
object.__setattr__(settings, "db_path", old)

run(db.init())
with sqlite3.connect(old) as conn:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
check("old db: init adds over_budget", "over_budget" in cols)
check("old db: and keeps the history", run(db.get_run("old1"))["kind"], "review")
check("old db: a new run can be claimed", run(db.claim_run(**row("new1"))), True)
check("old db: and starts under budget", run(db.get_run("new1"))["over_budget"], 0)
run(db.add_repo("acme/web"))
check("old db: a repo can be added despite the old NOT NULL columns",
      [r["repo"] for r in run(db.list_repos())], ["acme/api", "acme/web"])
check("old db: tables v1 no longer uses are left alone",
      sqlite3.connect(old).execute("SELECT count(*) FROM sqlite_master WHERE name='releases'").fetchone()[0], 1)

# A real error in a migration is raised, not swallowed as "already applied".
broken = Path(tmp.name) / "broken.db"
with sqlite3.connect(broken) as conn:
    conn.execute("CREATE VIEW runs AS SELECT 1 AS id")
object.__setattr__(settings, "db_path", broken)
try:
    run(db.init())
    check("init: an error other than 'duplicate column' is raised", False)
except sqlite3.Error:
    check("init: an error other than 'duplicate column' is raised", True)

print()
print(f"{len(fails)} failed" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
