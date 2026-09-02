"""The goal loop's state, against a real database: the migration, the transitions the
register owns, and the mutual exclusion the whole design leans on.

Three claims worth a throwaway SQLite file rather than a stub:

1. **The migration lands on every kind of database.** A fresh `init()` gets `goal_sha` from
   SCHEMA; a database that predates the goal loop gets it from MIGRATIONS; and a database
   from the textarea era gets its retired `goal` column dropped rather than resurrected —
   the migration list no longer contains `ADD COLUMN goal`, or the drop would make that add
   succeed again on every restart.
2. **The transitions live at the writer.** `repos.apply_goal_file` turns what the sync
   observed about `.factory/goal.md` into state — an unchanged SHA is a no-op, a changed one
   re-arms, a vanished one clears — and `reactivate` is the only human door. Getting these
   wrong is how a `met` project quietly wakes up every sync, or a stalled one stays dead
   after its goal file was rewritten.
3. **A plan run blocks dispatch.** `has_active_run` counts `kind='plan'` (unlike
   `provision`), which is what makes a planning repo unable to double-plan or start a build
   mid-plan without any extra state.

Run it directly, no framework needed:

    .venv/bin/python -m control.goal_state_test
"""
import asyncio
import sys
import tempfile
from pathlib import Path

import aiosqlite

from control import db, repos
from control.config import settings

fails = []


def check(name, got, want=True):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n       got={got!r} want={want!r}"))
    if not ok:
        fails.append(name)


def run(coro):
    return asyncio.run(coro)


tmp = tempfile.TemporaryDirectory()
object.__setattr__(settings, "db_path", Path(tmp.name) / "factory.db")


# ---------- the migration reaches a database that predates the goal columns

async def make_old_register():
    """The repos table exactly as it was before the goal loop existed."""
    async with aiosqlite.connect(settings.db_path) as conn:
        await conn.execute(
            "CREATE TABLE repos (repo TEXT PRIMARY KEY, added_at TEXT NOT NULL, "
            "golden TEXT, provision_status TEXT NOT NULL DEFAULT 'none', agent TEXT)"
        )
        await conn.execute(
            "INSERT INTO repos (repo, added_at) VALUES ('acme/api', '2026-01-01T00:00:00+00:00')"
        )
        await conn.commit()


run(make_old_register())
run(db.init())
rows = run(db.list_repos())
check("migration: an old register gains the goal columns",
      {"goal_sha", "goal_state", "plan_stalls", "last_planned_at"} <= set(rows[0]))
check("migration: with the states a goal-less repo should have",
      (rows[0]["goal_sha"], rows[0]["goal_state"], rows[0]["plan_stalls"], rows[0]["last_planned_at"]),
      (None, "none", 0, None))
check("migration: the retired textarea goal column is not created on the way",
      "goal" in rows[0], False)
run(db.init())
check("migration: a second init is a no-op, not a reset",
      run(db.list_repos())[0]["goal_state"], "none")

# A database from the textarea era: the goal loop's columns exist and `goal` holds prose.
# The migration must drop the prose without touching its neighbours. (On a SQLite too old
# for DROP COLUMN the drop fails into init()'s ignore and the dead column stays — this test
# also documents which behaviour the bundled SQLite actually has.)

async def make_textarea_era():
    async with aiosqlite.connect(settings.db_path) as conn:
        await conn.execute("DROP TABLE repos")
        await conn.execute(
            "CREATE TABLE repos (repo TEXT PRIMARY KEY, added_at TEXT NOT NULL, "
            "golden TEXT, provision_status TEXT NOT NULL DEFAULT 'none', agent TEXT, "
            "goal TEXT, goal_state TEXT NOT NULL DEFAULT 'none', "
            "plan_stalls INTEGER NOT NULL DEFAULT 0, last_planned_at TEXT)"
        )
        await conn.execute(
            "INSERT INTO repos (repo, added_at, goal, goal_state) "
            "VALUES ('acme/api', '2026-01-01T00:00:00+00:00', 'old prose goal', 'met')"
        )
        await conn.commit()


run(make_textarea_era())
run(db.init())
rows = run(db.list_repos())
check("migration: a textarea-era register loses the prose column",
      "goal" in rows[0], False)
check("migration: and keeps the loop state it had earned",
      (rows[0]["goal_sha"], rows[0]["goal_state"]), (None, "met"))

# A fresh database gets the same shape from SCHEMA alone — repos_test.py asserts the exact
# column list on that path, so here it is enough that both paths named the columns the same.

run(repos.load())
run(db.set_plan_state("acme/api", state="none"))
run(repos.load())


# ---------- goal_file_transition: the pure decision, every case

t = repos.goal_file_transition
check("pure: still no file -> no transition", t({"goal_sha": None}, None), None)
check("pure: unchanged sha -> no transition", t({"goal_sha": "abc"}, "abc"), None)
check("pure: first sha -> active", t({"goal_sha": None}, "abc"), "active")
check("pure: changed sha -> active, whatever the state was",
      t({"goal_sha": "abc", "goal_state": "met"}, "def"), "active")
check("pure: file deleted -> the goal is gone", t({"goal_sha": "abc"}, None), "none")


# ---------- apply_goal_file: the transition rides on the write

def state(repo="acme/api"):
    r = repos.row(repo)
    return (r.get("goal_sha"), r.get("goal_state"), r.get("plan_stalls"))


run(repos.apply_goal_file("acme/api", "sha-one"))
check("sync: a committed goal file arms the loop by itself", state(), ("sha-one", "active", 0))

run(db.set_plan_state("acme/api", state="met", stalls=0))
run(repos.load())
run(repos.apply_goal_file("acme/api", "sha-one"))
check("sync: an unchanged file is a no-op — a met repo sleeps through every sync",
      state()[1], "met")

run(repos.apply_goal_file("acme/api", "sha-two"))
check("sync: a commit that changes the file re-arms a met goal", state()[1], "active")

run(db.set_plan_state("acme/api", state="stalled", stalls=2,
                      last_planned_at="2026-08-27T10:00:00+00:00"))
run(repos.load())
run(repos.apply_goal_file("acme/api", "sha-three"))
check("sync: a changed file re-arms a stalled goal and resets the count",
      state(), ("sha-three", "active", 0))
check("sync: and clears the cooldown a previous goal earned",
      repos.row("acme/api").get("last_planned_at"), None)

run(repos.apply_goal_file("acme/api", None))
check("sync: a deleted goal file clears the goal entirely", state(), (None, "none", 0))
run(repos.apply_goal_file("acme/api", None))
check("sync: clearing an already-clear goal is a no-op", state(), (None, "none", 0))

try:
    run(repos.apply_goal_file("nobody/watches", "anything"))
    check("sync: an unwatched repo is refused", "no error", "ValueError")
except ValueError:
    check("sync: an unwatched repo is refused", True)


# ---------- reactivate: the door out of met and stalled

run(repos.apply_goal_file("acme/api", "sha-final"))
run(db.set_plan_state("acme/api", state="stalled", stalls=2))
run(repos.load())
run(repos.reactivate("acme/api"))
check("replan: a stalled goal goes back to active with the count reset",
      state(), ("sha-final", "active", 0))

try:
    run(repos.apply_goal_file("acme/api", None))
    run(repos.reactivate("acme/api"))
    check("replan: a repo with no goal file is refused", "no error", "ValueError")
except ValueError:
    check("replan: a repo with no goal file is refused", True)


# ---------- the plan loop's own bookkeeping

run(repos.apply_goal_file("acme/api", "sha-final"))
run(repos.record_plan_start("acme/api"))
check("start: dispatch stamps the cooldown clock",
      bool(repos.row("acme/api").get("last_planned_at")))
run(repos.record_plan_outcome("acme/api", "met", 0))
check("outcome: recorded without touching the goal file's sha",
      state(), ("sha-final", "met", 0))


# ---------- a plan run holds the repo, the way provision deliberately does not

run(db.create_run(
    id="pl1", repo="acme/api", issue_number=0, status="running", kind="plan",
    created_at=db.utcnow(),
))
check("dispatch: a plan run in flight claims the repo", run(db.has_active_run("acme/api")))
run(db.update_run("pl1", status="succeeded"))
check("dispatch: and releases it when terminal", run(db.has_active_run("acme/api")), False)


tmp.cleanup()
print()
print(f"{len(fails)} failed" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
