"""The register of watched repos: the table, the cache over it, and the seed that fills it.

Connecting a repo stopped being an `.env` edit plus a systemd restart, which means the list
the poller reads can now change while it is running. Three things have to hold for that to be
safe, and each is a section below:

1. **The seed is additive.** `FACTORY_REPOS` fills an empty register and never clobbers one,
   because a deployment that has since connected repos through the API reboots too. Dropping
   an entry from `FACTORY_REPOS` must not silently unwatch a repo either — deleting is an
   explicit act.
2. **The cache and the table cannot disagree.** Every write goes through this module and
   reloads. A cache that lags is a poller dispatching to a repo nobody watches any more.
3. **`init()` really produces the schema.** Worth asserting rather than assuming: `SCHEMA`
   runs before `MIGRATIONS`, and `MIGRATIONS` contains two `DROP TABLE` statements — so a
   table added to `SCHEMA` under a name the migrations drop would be created and destroyed on
   every single boot, silently, forever.

This one does touch SQLite, against a throwaway file. That is the point: the failure it
guards against lives in the interaction between `SCHEMA` and `MIGRATIONS`, and a stubbed
database cannot have it.

Run it directly, no framework needed:

    .venv/bin/python -m control.repos_test
"""
import asyncio
import sys
import tempfile
from pathlib import Path

from control import db, repos
from control.config import settings

fails = []


def check(name, got, want=True):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n       got={got!r} want={want!r}"))
    if not ok:
        fails.append(name)


tmp = tempfile.TemporaryDirectory()
# `settings` is frozen, and `db.connect()` reads this attribute per call rather than binding
# it at import — so pointing it at a throwaway file is enough, and the real database is never
# opened by this test.
object.__setattr__(settings, "db_path", Path(tmp.name) / "factory.db")
asyncio.run(db.init())


def run(coro):
    return asyncio.run(coro)


# ---------- the schema survives its own migrations

async def table_names():
    async with db.connect() as conn:
        async with conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'") as cur:
            return sorted(r["name"] for r in await cur.fetchall())


tables = run(table_names())
check("schema: init creates the repos table", "repos" in tables)
check("schema: and the snapshots table survives the migrations that drop its predecessors",
      "snapshots" in tables)
check("schema: the tables the migrations retire are gone",
      [t for t in ("goldens", "agents") if t in tables], [])

# Running it twice is what a restart does.
run(db.init())
check("schema: a second init is a no-op, not a reset", run(table_names()), tables)


# ---------- validation

check("valid: owner/name is what GitHub means", repos.valid("mithril-studio/software-factory"))
check("valid: dots, dashes and underscores in the name are fine", repos.valid("acme/my_api.v2"))
check("valid: a bare name names no owner", repos.valid("software-factory"), False)
check("valid: a URL is not a repo", repos.valid("https://github.com/a/b"), False)
check("valid: a third segment is not a repo", repos.valid("a/b/c"), False)
check("valid: the empty string is not a repo", repos.valid(""), False)
check("valid: neither is whitespace", repos.valid("   "), False)
# The old FACTORY_REPOS syntax. `config._repos()` strips the `=agent` half before this ever
# sees it; if that ever stops happening, this is the check that goes red.
check("valid: a legacy owner/repo=agent entry is not a repo name", repos.valid("a/b=pi"), False)


# ---------- seed

def seed(*entries):
    object.__setattr__(settings, "repos", tuple(entries))
    run(repos.seed())
    return repos.watched()


check("seed: FACTORY_REPOS fills an empty register",
      seed("acme/api", "acme/web"), ("acme/api", "acme/web"))
check("seed: and the rows carry everything the register knows",
      sorted(repos.rows()[0]), ["added_at", "agent", "golden", "provision_status", "repo"])
check("seed: a repo nobody could parse is skipped rather than watched",
      seed("acme/api", "not a repo", "acme/web"), ("acme/api", "acme/web"))

# The two ways the seed could destroy something on an ordinary restart.
run(repos.record_golden("acme/api", "golden-acme-api", "ready"))
seed("acme/api", "acme/web")
check("seed: re-seeding does not reset a golden the repo has already been provisioned",
      repos.rows()[0]["golden"], "golden-acme-api")
check("seed: nor its provision status", repos.rows()[0]["provision_status"], "ready")

run(repos.add("connected/through-the-api"))
check("seed: a repo connected through the API survives a restart",
      "connected/through-the-api" in seed("acme/api", "acme/web"), True)
check("seed: dropping an entry from FACTORY_REPOS does not unwatch it either",
      "acme/web" in seed("acme/api"), True)


# ---------- add, remove, and the cache over them

before = repos.watched()
run(repos.add("new/repo"))
check("add: the new repo is watched immediately, with no reload by the caller",
      repos.watched()[-1], "new/repo")
check("add: and nothing else moved", repos.watched()[:len(before)], before)
check("add: adding it twice leaves one entry",
      (run(repos.add("new/repo")), repos.watched().count("new/repo"))[1], 1)

check("remove: says whether there was anything to remove", run(repos.remove("new/repo")), True)
check("remove: and the cache no longer holds it", "new/repo" in repos.watched(), False)
check("remove: removing something unwatched is False, not an error",
      run(repos.remove("nobody/knows")), False)

# The cache is the only thing anything reads synchronously, so a write that did not reload it
# would leave the poller dispatching to a repo that is no longer registered.
check("cache: what the cache holds is what the table holds",
      repos.watched(), tuple(r["repo"] for r in run(db.list_repos())))
run(repos.record_golden("acme/api", None, "failed"))
check("cache: recording a provisioning result refreshes the row too",
      (repos.rows()[0]["golden"], repos.rows()[0]["provision_status"]), (None, "failed"))

check("order: the register is oldest first, so the poller works repos in the order they arrived",
      repos.watched()[0], "acme/api")


# ---------- runs outlive the repo

# Disconnecting a repo is a configuration change, not a redaction. The runs are the ledger of
# what this deployment spent and shipped, and they stay true after nobody is watching.
run(db.create_run(
    id="r1", repo="acme/web", issue_number=1, status="succeeded", created_at=db.utcnow(),
))
run(repos.remove("acme/web"))
check("removal: the repo's runs are kept", [r["id"] for r in run(db.list_runs())], ["r1"])
check("removal: and it is no longer watched", "acme/web" in repos.watched(), False)


# ---------- warming a golden does not stop the repo it was connected for

# `POST /api/repos` starts a provisioning run the moment a repo is connected, and the poller
# skips any repo with a run in flight. Counting the warm-up there made a repo arriving with
# queued issues sit idle for the whole install — the exact opposite of "dispatchable the moment
# it is registered", which is what the two-tier golden design exists to make true.
run(db.create_run(
    id="p1", repo="acme/api", issue_number=0, status="running", kind="provision",
    created_at=db.utcnow(),
))
check("dispatch: a warm-up in flight does not claim the repo",
      run(db.has_active_run("acme/api")), False)
run(db.create_run(
    id="b1", repo="acme/api", issue_number=7, status="running", kind="build",
    created_at=db.utcnow(),
))
check("dispatch: a build in flight still does", run(db.has_active_run("acme/api")), True)


tmp.cleanup()
print()
print(f"{len(fails)} failed" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
