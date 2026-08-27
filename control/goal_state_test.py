"""The goal loop's state, against a real database: the migration, the transitions the
register owns, and the mutual exclusion the whole design leans on.

Three claims worth a throwaway SQLite file rather than a stub:

1. **The migration lands on both kinds of database.** A fresh `init()` gets the goal columns
   from SCHEMA; a database created before they existed gets them from MIGRATIONS. The two
   paths must agree on names and defaults, or a deployment upgrades into a poller reading
   columns that are not there.
2. **The transitions live at the writer.** `repos.set_goal` decides state from the edit —
   unchanged text is a no-op, changed text re-arms, empty text clears — and `reactivate` is
   the only other human door. Getting these wrong is how a `met` project quietly wakes up,
   or a stalled one stays dead after its goal was rewritten.
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
      {"goal", "goal_state", "plan_stalls", "last_planned_at"} <= set(rows[0]))
check("migration: with the states a goal-less repo should have",
      (rows[0]["goal"], rows[0]["goal_state"], rows[0]["plan_stalls"], rows[0]["last_planned_at"]),
      (None, "none", 0, None))
run(db.init())
check("migration: a second init is a no-op, not a reset",
      run(db.list_repos())[0]["goal_state"], "none")

# A fresh database gets the same shape from SCHEMA alone — repos_test.py asserts the exact
# column list on that path, so here it is enough that both paths named the columns the same.

run(repos.load())


# ---------- set_goal: the transition rides on the write

def state(repo="acme/api"):
    r = repos.row(repo)
    return (r.get("goal"), r.get("goal_state"), r.get("plan_stalls"))


run(repos.set_goal("acme/api", "a CLI that frobs"))
check("set: writing a goal activates it", state(), ("a CLI that frobs", "active", 0))

run(db.set_plan_state("acme/api", state="met", stalls=0))
run(repos.load())
run(repos.set_goal("acme/api", "a CLI that frobs"))
check("set: unchanged text is a no-op — saving the form twice cannot wake a met goal",
      state()[1], "met")
run(repos.set_goal("acme/api", "  a CLI that frobs  "))
check("set: whitespace does not count as a change either", state()[1], "met")

run(repos.set_goal("acme/api", "a CLI that frobs and logs"))
check("set: changed text re-arms a met goal", state()[1], "active")

run(db.set_plan_state("acme/api", state="stalled", stalls=2,
                      last_planned_at="2026-08-27T10:00:00+00:00"))
run(repos.load())
run(repos.set_goal("acme/api", "third goal"))
check("set: changed text re-arms a stalled goal and resets the count",
      state(), ("third goal", "active", 0))
check("set: and clears the cooldown a previous goal earned",
      repos.row("acme/api").get("last_planned_at"), None)

run(repos.set_goal("acme/api", ""))
check("set: empty text clears the goal entirely", state(), (None, "none", 0))
run(repos.set_goal("acme/api", None))
check("set: clearing an already-clear goal is a no-op", state(), (None, "none", 0))

try:
    run(repos.set_goal("nobody/watches", "anything"))
    check("set: an unwatched repo is refused", "no error", "ValueError")
except ValueError:
    check("set: an unwatched repo is refused", True)


# ---------- reactivate: the door out of met and stalled

run(repos.set_goal("acme/api", "final goal"))
run(db.set_plan_state("acme/api", state="stalled", stalls=2))
run(repos.load())
run(repos.reactivate("acme/api"))
check("replan: a stalled goal goes back to active with the count reset",
      state(), ("final goal", "active", 0))

try:
    run(repos.set_goal("acme/api", None))
    run(repos.reactivate("acme/api"))
    check("replan: a repo with no goal is refused", "no error", "ValueError")
except ValueError:
    check("replan: a repo with no goal is refused", True)


# ---------- the plan loop's own bookkeeping

run(repos.set_goal("acme/api", "final goal"))
run(repos.record_plan_start("acme/api"))
check("start: dispatch stamps the cooldown clock",
      bool(repos.row("acme/api").get("last_planned_at")))
run(repos.record_plan_outcome("acme/api", "met", 0))
check("outcome: recorded without touching the goal text",
      state(), ("final goal", "met", 0))


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
