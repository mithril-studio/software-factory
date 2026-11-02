"""Releases: the schedule, the freeze, and what a release claims to carry.

Each of these fails quietly if it is wrong. A schedule that double-fires promotes twice in a
minute and the test server restarts under whoever is reading it; one that is off by an hour
across a daylight-saving change releases at 08:00 for half the year. A freeze that never
lifts stops releases for good, and one that lifts early moves the test server mid-review. A
release that claims the wrong items sends a reviewer to check work that is not on the server.
And a promotion that forces a diverged `test` throws away whatever somebody put there.

Run it directly, no framework needed:

    .venv/bin/python -m control.release_test
"""
import asyncio
import datetime as dt
import inspect
import sys
import tempfile
from pathlib import Path

import httpx

from control import db, github, release, repos, runner
from control.config import settings

fails: list[str] = []


def check(name, got, want=True):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n       got={got!r} want={want!r}"))
    if not ok:
        fails.append(name)


def run(coro):
    return asyncio.run(coro)


UTC = dt.timezone.utc
AMS = "Europe/Amsterdam"
TIMES = release.parse_times("09:00,11:30,14:00,16:00")


def at(text):
    return dt.datetime.fromisoformat(text).astimezone(UTC)


# ---------- the schedule

check("times parse sorted, junk dropped",
      release.parse_times("16:00, 09:00,nope,25:99"), (dt.time(9), dt.time(16)))
check("before the first moment of the first day nothing is owed",
      release.due_moment(at("2026-06-01T08:59:00+02:00"), TIMES, AMS, "2026-06-01T06:00:00+00:00"),
      None)
check("at 09:00 local in summer the 09:00 moment is owed",
      release.due_moment(at("2026-06-01T09:00:30+02:00"), TIMES, AMS, None),
      at("2026-06-01T09:00:00+02:00"))
check("a release created after the moment spends it — no double fire",
      release.due_moment(at("2026-06-01T09:01:30+02:00"), TIMES, AMS, "2026-06-01T07:00:10+00:00"),
      None)
check("a release reviewed after a moment spends it too, so 09:00 does not fire at 10:00",
      release.due_moment(at("2026-06-01T10:00:00+02:00"), TIMES, AMS, "2026-06-01T07:45:00+00:00"),
      None)
check("the next moment is owed when it comes",
      release.due_moment(at("2026-06-01T11:31:00+02:00"), TIMES, AMS, "2026-06-01T07:45:00+00:00"),
      at("2026-06-01T11:30:00+02:00"))
check("past midnight the moment owed is yesterday's last one",
      release.due_moment(at("2026-06-02T00:30:00+02:00"), TIMES, AMS, "2026-06-01T10:00:00+00:00"),
      at("2026-06-01T16:00:00+02:00"))
check("and it is spent once a release follows it",
      release.due_moment(at("2026-06-02T00:30:00+02:00"), TIMES, AMS, "2026-06-01T14:00:05+00:00"),
      None)
# 2026-03-29: Amsterdam goes from +01:00 to +02:00 at 02:00 local.
check("spring forward: 09:00 is still 09:00 local (07:00 UTC)",
      release.due_moment(at("2026-03-29T07:00:30+00:00"), TIMES, AMS, None),
      at("2026-03-29T07:00:00+00:00"))
check("spring forward: at 07:59 UTC the day before's wall time would have been 08:59 — not owed",
      release.due_moment(at("2026-03-29T06:59:00+00:00"), TIMES, AMS, "2026-03-28T15:05:00+00:00"),
      None)
# 2026-10-25: back from +02:00 to +01:00 at 03:00 local.
check("fall back: 09:00 local is 08:00 UTC",
      release.due_moment(at("2026-10-25T08:00:30+00:00"), TIMES, AMS, "2026-10-24T14:05:00+00:00"),
      at("2026-10-25T08:00:00+00:00"))
check("fall back: an hour earlier is not 09:00 any more",
      release.due_moment(at("2026-10-25T07:30:00+00:00"), TIMES, AMS, "2026-10-24T14:05:00+00:00"),
      None)
check("a nonexistent wall time still lands on a real instant",
      release.due_moment(at("2026-03-29T05:00:00+00:00"), (dt.time(2, 30),), AMS, None)
      is not None, True)
check("no times configured means nothing is ever owed",
      release.due_moment(at("2026-06-01T12:00:00+00:00"), (), AMS, None), None)

# ---------- the freeze

NOW = at("2026-06-02T12:00:00+00:00")


def rel(status, promoted_at="2026-06-02T09:00:00+00:00"):
    return {"id": 1, "status": status, "created_at": "2026-06-02T08:59:00+00:00",
            "promoted_at": promoted_at}


check("no release, no freeze", release.frozen(None, NOW, 86400), False)
for status in ("assembling", "promoted", "deploying", "deployed", "accepting", "accepted", "rejected"):
    check(f"a {status} release freezes", release.frozen(rel(status), NOW, 86400), True)
for status in db.RELEASE_TERMINAL:
    check(f"a {status} release does not", release.frozen(rel(status), NOW, 86400), False)
check("a standing release past its timeout no longer freezes",
      release.frozen(rel("promoted"), NOW, 3600), False)
check("a rejected release never times out",
      release.frozen(rel("rejected"), NOW, 3600), True)
check("an in-flight release never times out",
      release.frozen(rel("deploying"), NOW, 3600), True)

# ---------- promotion failures

check("a 422 is a diverged release branch, and says nothing was forced",
      "Nothing was forced" in release.promotion_failure(422, "Update is not a fast forward", "test"),
      True)
check("a 403 is a permission problem",
      "may not write" in release.promotion_failure(403, "Resource not accessible", "test"), True)

# ---------- the table

tmp = tempfile.TemporaryDirectory()
object.__setattr__(settings, "db_path", Path(tmp.name) / "factory.db")
object.__setattr__(settings, "log_dir", Path(tmp.name) / "logs")
settings.log_dir.mkdir(parents=True, exist_ok=True)
run(db.init())
run(db.init())  # a restart


async def merged_run(rid, number, sha, repo="acme/api"):
    await db.create_run(
        id=rid, repo=repo, issue_number=number, issue_title=f"issue {number}",
        status="succeeded", kind="review", pr_url=f"https://github.com/{repo}/pull/{100 + number}",
        merged_sha=sha, created_at=db.utcnow(), finished_at=db.utcnow(),
    )


run(merged_run("r1", 1, "sha1"))
run(merged_run("r2", 2, "sha2"))
run(merged_run("r2b", 2, "sha2"))  # the same merge recorded twice
run(merged_run("x1", 9, "shaX", repo="acme/web"))
run(db.create_run(id="r3", repo="acme/api", issue_number=3, status="succeeded",
                  created_at=db.utcnow()))  # never merged

items = run(db.unreleased_items("acme/api"))
check("unreleased items are the repo's merged squash commits, once each",
      [i["merged_sha"] for i in items], ["sha1", "sha2"])

r = run(db.create_release("acme/api", test_sha="head1"))
check("a release is born assembling", r["status"], "assembling")


def refused(rid, to, **fields):
    try:
        run(db.transition_release(rid, to, **fields))
    except ValueError as exc:
        return str(exc)
    return None


check("assembling may not jump to deployed", refused(r["id"], "deployed") is not None, True)
check("an unknown column is refused", "unknown" in (refused(r["id"], "promoted", bogus=1) or ""), True)
run(db.add_release_items(r["id"], [items[0] | {"summary": "first"}]))
run(db.transition_release(r["id"], "promoted", promoted_at=db.utcnow()))
check("a carried item is no longer unreleased",
      [i["merged_sha"] for i in run(db.unreleased_items("acme/api"))], ["sha2"])
run(db.transition_release(r["id"], "reviewed", reviewed_at=db.utcnow()))
check("reviewed is terminal", refused(r["id"], "expired") is not None, True)
check("a failed release is terminal", db.RELEASE_TRANSITIONS.get("failed"), None)

# ---------- assemble and promote, against a fake GitHub

run(repos.add("acme/api"))
branches = {"main": "head2"}
moves: list[tuple] = []
compare_result = ["sha2", "other"]


async def fake_branch_sha(repo, branch):
    return branches.get(branch)


async def fake_default(repo):
    return "main"


async def fake_checks(repo, sha, timeout=900, interval=15):
    return (True, "1 checks passed", [])


async def fake_create(repo, branch, sha):
    moves.append(("create", branch, sha))
    branches[branch] = sha


async def fake_update(repo, branch, sha):
    moves.append(("update", branch, sha))
    if branch == "test" and branches.get("test") == "diverged":
        req = httpx.Request("PATCH", "https://api.github.com/x")
        raise httpx.HTTPStatusError(
            "could not move test: 422 Update is not a fast forward", request=req,
            response=httpx.Response(422, request=req),
        )
    branches[branch] = sha


async def fake_between(repo, base, head):
    return list(compare_result)


async def fake_pr(repo, number):
    return {"title": f"PR {number}", "body": "Adds the thing.\n\nCloses #2"}


async def fake_get_issue(repo, number):
    return {"number": number, "title": "", "labels": [], "body": (
        "## Acceptance criteria\n```yaml\n- id: AC1\n  mode: test\n  statement: \"works\"\n"
        "  verify: \"t.py\"\n```\n")}


github.get_issue = fake_get_issue
github.branch_sha = fake_branch_sha
github.default_branch = fake_default
github.checks_green = fake_checks
github.create_branch = fake_create
github.update_branch = fake_update
github.commits_between = fake_between
github.pull_request = fake_pr


async def assemble_and_wait(repo):
    row, why = await release.assemble(repo)
    if row is not None:
        await asyncio.gather(*list(release._inflight.values()))
        row = await db.get_release(row["id"])
    return row, why


row, why = run(assemble_and_wait("acme/api"))
check("the first release creates the release branch at the integration head",
      moves, [("create", "test", "head2")])
check("and is promoted", row["status"], "promoted")
check("with no previous position recorded", row["base_sha_before"], None)
first_items = run(db.release_items(row["id"]))
check("the first release carries every unreleased item", [i["merged_sha"] for i in first_items], ["sha2"])
check("each item quotes its pull request", first_items[0]["summary"], "Adds the thing. Closes #2")
check("and freezes the issue's criteria as they stood",
      __import__("json").loads(first_items[0]["criteria"])[0]["statement"], "works")

try:
    run(release.assemble("acme/api"))
    frozen_error = None
except release.Frozen as exc:
    frozen_error = str(exc)
check("an unreviewed release freezes the repo against another", frozen_error is not None, True)
run(release.mark_reviewed(row["id"]))

row, why = run(assemble_and_wait("acme/api"))
check("nothing new on the integration branch means no release", (row, "already at" in why), (None, True))

run(merged_run("r4", 4, "sha4"))
run(merged_run("r5", 5, "sha5"))
branches["main"] = "head3"
compare_result[:] = ["sha4"]  # sha5 merged after head3 was read
moves.clear()
row, why = run(assemble_and_wait("acme/api"))
check("the next release fast-forwards rather than creates", moves, [("update", "test", "head3")])
check("and records where test was before", row["base_sha_before"], "head2")
check("it carries only the items in the range that moved",
      [i["merged_sha"] for i in run(db.release_items(row["id"]))], ["sha4"])
check("the item left out is still unreleased",
      [i["merged_sha"] for i in run(db.unreleased_items("acme/api"))], ["sha5"])
run(release.mark_reviewed(row["id"]))

branches["main"] = "head4"
branches["test"] = "diverged"
row, why = run(assemble_and_wait("acme/api"))
check("a diverged release branch fails the release", row["status"], "failed")
check("and says why", "diverged" in (row["error"] or ""), True)
check("a failed promotion claims no items", run(db.release_items(row["id"])), [])
check("a failed release does not freeze", release.frozen(row, release._now(), 86400), False)

branches["test"] = "head3"


async def red_checks(repo, sha, timeout=900, interval=15):
    return (False, "checks failed: gates=failure", ["gates=failure"])


github.checks_green = red_checks
row, why = run(assemble_and_wait("acme/api"))
check("red CI on the head fails the release before anything moves",
      (row["status"], branches["test"]), ("failed", "head3"))
github.checks_green = fake_checks

# ---------- expiry and recovery

old = run(db.create_release("acme/web", test_sha="w1"))
run(db.transition_release(old["id"], "promoted", promoted_at="2026-01-01T00:00:00+00:00"))
check("a release nobody read expires", run(release.expire_stale()), [old["id"]])
check("and lifts the freeze", release.frozen(run(db.get_release(old["id"])), release._now(), 86400),
      False)
stuck = run(db.create_release("acme/web", test_sha="w2"))
check("a release left assembling by a restart is failed at startup",
      run(release.recover()), [stuck["id"]])

# ---------- wiring

merge_src = inspect.getsource(runner._merge)
check("a merge records its squash commit on the run",
      "merged_sha=merged[\"sha\"]" in merge_src, True)
for name, fn in (("build", runner._execute), ("review", runner._execute_review)):
    check(f"the {name} path passes its run id to _merge", "run_id=run_id" in inspect.getsource(fn), True)
tick_src = inspect.getsource(release._tick)
check("the schedule reads the latest release's last update",
      "latest[\"updated_at\"]" in tick_src, True)
check("assembly is gated on FACTORY_RELEASE, expiry is not",
      tick_src.find("expire_stale") < tick_src.find("settings.release_enabled"), True)

print()
print(f"{len(fails)} failed: {', '.join(fails)}" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
