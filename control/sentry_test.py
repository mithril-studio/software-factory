"""The Sentry integration: the mapping, the mirror, and the provision-once guarantees.

Three failure modes this guards, each of which would be silent in production:

1. **A drifting mapping.** `bug_row_from_issue` is the one place Sentry's JSON becomes our
   columns. A key it emits that `BUG_COLUMNS` does not hold would make every sync raise —
   so the mapping's output is checked against the column set, not just spot fields.
2. **A wiring issue filed twice, or a project created twice.** Provisioning records each
   step in the register the moment it happens; the pure `should_*` decisions plus the
   recorded state are what make a crash resumable instead of repeatable.
3. **A template nobody can parse.** The wiring issue's acceptance criteria are only worth
   filing if `runner.parse_criteria` can read them — a malformed block merges unreviewed,
   which factory-compose calls worse than no block at all. The rendered template is fed to
   the real parser here, so editing it into unparseability fails CI instead of a review.

Run it directly, no framework needed:

    .venv/bin/python -m control.sentry_test
"""
import asyncio
import sys
import tempfile
from pathlib import Path

from control import db, repos, sentry
from control.config import settings

fails = []


def check(name, got, want=True):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n       got={got!r} want={want!r}"))
    if not ok:
        fails.append(name)


tmp = tempfile.TemporaryDirectory()
object.__setattr__(settings, "db_path", Path(tmp.name) / "factory.db")
asyncio.run(db.init())


def run(coro):
    return asyncio.run(coro)


# ---------- the slug is derived, lowercase, and bounded

check("slug: owner and name both, the way goldens are named",
      sentry.project_slug("mithril-studio/software-factory"), "mithril-studio-software-factory")
check("slug: anything Sentry would refuse becomes a dash",
      sentry.project_slug("Acme/my_api.v2"), "acme-my-api-v2")
check("slug: never longer than Sentry's 50, and never ends on the cut",
      sentry.project_slug("o/" + "a" * 60)[:2] == "o-" and len(sentry.project_slug("o/" + "a" * 60)) <= 50)

check("bug id: derived from identity, so re-syncs land on one row",
      sentry.bug_id("a/b", "123"), sentry.bug_id("a/b", "123"))
check("bug id: and distinct identities never collide into one",
      sentry.bug_id("a/b", "123") == sentry.bug_id("a/c", "123"), False)


# ---------- the mapping survives Sentry's payload being Sentry's business

ISSUE = {
    "id": "6001", "shortId": "APP-1", "title": "TypeError: x is not a function",
    "culprit": "app/checkout in submit", "level": "error", "status": "unresolved",
    "substatus": "ongoing", "count": "42", "userCount": 7,
    "firstSeen": "2026-09-01T10:00:00Z", "lastSeen": "2026-09-02T09:00:00Z",
    "permalink": "https://acme.sentry.io/issues/6001/",
}

row = sentry.bug_row_from_issue("acme/app", ISSUE)
check("map: every key it writes is a column the table holds",
      sorted(row), sorted(db.BUG_COLUMNS))
check("map: Sentry's count is a string on the wire, an integer here", row["count"], 42)
check("map: user count too", row["user_count"], 7)
check("map: the row carries the repo it was synced for", row["repo"], "acme/app")
check("map: an empty payload maps rather than raises",
      sentry.bug_row_from_issue("acme/app", {})["count"], 0)
check("map: a title longer than the cap is truncated, not refused",
      len(sentry.bug_row_from_issue("a/b", {"title": "x" * 999})["title"]), 300)
check("map: a count that is not a number is zero, not a crash",
      sentry.bug_row_from_issue("a/b", {"count": "many"})["count"], 0)


# ---------- the wiring issue's criteria block parses with the real parser

from control import runner  # noqa: E402 - heavy import, deliberate: the real parser is the point

body = sentry.WIRING_ISSUE_TEMPLATE.format(dsn="https://k@o.ingest.sentry.io/1", project="acme-app")
criteria = runner.parse_criteria(body)
check("template: the rendered criteria block parses", len(criteria) > 0)
check("template: and carries a blocking criterion, so the review actually gates",
      any(c["mode"] in runner.BLOCKING_MODES for c in criteria))
check("template: the DSN lands in the body verbatim", "https://k@o.ingest.sentry.io/1" in body)


# ---------- provision-once: the decisions, then the recorded state that feeds them

check("provision: a repo with no project needs one", sentry.should_provision({"repo": "a/b"}))
check("provision: one with a project does not",
      sentry.should_provision({"sentry_project": "a-b"}), False)
check("wiring: no DSN yet means nothing to file",
      sentry.should_file_wiring({"repo": "a/b"}), False)
check("wiring: a DSN with no issue filed means file it",
      sentry.should_file_wiring({"sentry_dsn": "https://k@o/1"}), True)
check("wiring: a recorded issue means never again",
      sentry.should_file_wiring({"sentry_dsn": "https://k@o/1", "sentry_wiring_issue": 12}), False)

object.__setattr__(settings, "repos", ("acme/app",))
run(repos.seed())
run(repos.record_sentry("acme/app", "acme-app", "https://k@o.ingest.sentry.io/1"))
check("register: the cached row carries the project the moment it is recorded",
      repos.row("acme/app")["sentry_project"], "acme-app")
check("register: provisioning is now settled for this repo",
      sentry.should_provision(repos.row("acme/app")), False)
check("register: but the wiring issue is still owed",
      sentry.should_file_wiring(repos.row("acme/app")), True)

run(repos.record_sentry_wiring("acme/app", 34))
check("register: a recorded wiring issue closes the loop",
      sentry.should_file_wiring(repos.row("acme/app")), False)
check("register: and survives a re-seed like every other provisioned fact",
      (run(repos.seed()), repos.row("acme/app")["sentry_wiring_issue"])[1], 34)


# ---------- the mirror: one row per Sentry issue, refreshed rather than duplicated

run(db.upsert_bug(sentry.bug_row_from_issue("acme/app", ISSUE)))
run(db.upsert_bug(sentry.bug_row_from_issue("acme/app", {**ISSUE, "count": "43", "status": "resolved"})))
bugs = run(db.list_bugs())
check("upsert: a re-sync is an update, not a second row", len(bugs), 1)
check("upsert: and it carries what Sentry says now", (bugs[0]["count"], bugs[0]["status"]),
      (43, "resolved"))
try:
    run(db.upsert_bug({"id": "x", "repo": "a/b", "sentry_issue_id": "1",
                       "synced_at": db.utcnow(), "bogus": 1}))
    check("upsert: an unknown column is refused loudly", False)
except ValueError:
    check("upsert: an unknown column is refused loudly", True)

run(db.upsert_bug(sentry.bug_row_from_issue("acme/app", {
    **ISSUE, "id": "6002", "lastSeen": "2026-09-03T09:00:00Z",
})))
run(db.upsert_bug(sentry.bug_row_from_issue("other/repo", {**ISSUE, "id": "7001"})))
check("list: most recently seen first",
      [b["sentry_issue_id"] for b in run(db.list_bugs(repo="acme/app"))], ["6002", "6001"])
check("list: scoped to a repo when asked",
      {b["repo"] for b in run(db.list_bugs(repo="other/repo"))}, {"other/repo"})
check("list: unscoped sees every repo", len(run(db.list_bugs())), 3)


# ---------- the loop: per-repo failures are skipped, and off means off

calls = []


async def fake_list(slug, limit=sentry.SYNC_LIMIT):
    calls.append(slug)
    if slug == "broken-repo":
        raise RuntimeError("sentry is down for this one")
    return [ISSUE, {**ISSUE, "id": "6002"}]


run(repos.add("broken/repo"))
run(repos.record_sentry("broken/repo", "broken-repo", "https://k@o/2"))
run(repos.record_sentry_wiring("broken/repo", 1))
_real_list = sentry.list_issues
sentry.list_issues = fake_list
written = run(sentry.sync_once())
sentry.list_issues = _real_list
check("sync: a repo whose listing fails costs that repo, not the pass",
      ("acme-app" in calls, "broken-repo" in calls), (True, True))
check("sync: and the healthy repo's rows were still written", written, 2)

check("start: disabled by default, so nothing launches", settings.sentry_enabled, False)
sentry.start()
check("start: and the loop task stays unlaunched", sentry._task, None)


tmp.cleanup()
print()
print(f"{len(fails)} failed" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
