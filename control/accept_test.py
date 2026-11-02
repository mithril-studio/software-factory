"""Browser acceptance decides fail-closed, and a rejection files work the factory can build.

The tester is an agent, so the same rule holds as for the reviewer: it reports, the control
plane decides, and an `accept` must not survive an item the tester itself saw fail, an issue
it never mentioned, or one it could not verify. A rejection that files a repair issue the
dispatch gate then refuses would park a release forever behind an issue nobody can build — so
the repair re-cites the failed issues' own criteria, and this file checks it passes the gate.
And Playwright stays where it was put: `golden-tester` must never be what a build or review
boots, and the acceptance VM must be one the reconciler recognises and reaps.

Run it directly, no framework needed:

    .venv/bin/python -m control.accept_test
"""
import asyncio
import datetime as dt
import inspect
import json
import sys
import tempfile
from pathlib import Path

from control import accept, agents, db, github, release, runner
from control.config import settings

fails: list[str] = []


def check(name, got, want=True):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n       got={got!r} want={want!r}"))
    if not ok:
        fails.append(name)


def run(coro):
    return asyncio.run(coro)


ISSUES = [7, 9]


def verdict(*items, v="accept", findings=()):
    return {"verdict": v, "items": [
        {"issue": n, "status": s, "evidence": e} for n, s, e in items], "findings": list(findings)}


# ---------- deciding

check("every issue passing is accepted",
      accept.decide_acceptance(verdict((7, "pass", "saw 429"), (9, "pass", "saw it")), ISSUES)[0], True)
check("not_applicable with a reason counts as passing",
      accept.decide_acceptance(verdict((7, "pass", "ok"), (9, "not_applicable", "CI-only change")), ISSUES)[0],
      True)
check("not_applicable with no reason does not",
      accept.decide_acceptance(verdict((7, "pass", "ok"), (9, "not_applicable", "  ")), ISSUES)[:2],
      (False, "could not verify #9"))
ok, why, failed = accept.decide_acceptance(
    verdict((7, "fail", "500 on /login"), (9, "pass", "ok"), v="accept"), ISSUES)
check("an item the tester saw fail cannot be accepted, whatever it says overall", ok, False)
check("and is the one named for repair", failed, [7])
check("an issue nobody reported on is not accepted, and nothing is filed",
      accept.decide_acceptance(verdict((7, "pass", "ok")), ISSUES), (False, "the tester did not report on #9", []))
check("cannot_verify is not accepted, and nothing is filed",
      accept.decide_acceptance(verdict((7, "pass", "ok"), (9, "cannot_verify", "server down")), ISSUES)[2], [])
check("an unknown status is not accepted",
      accept.decide_acceptance(verdict((7, "pass", "ok"), (9, "looks fine", "x")), ISSUES)[0], False)
check("no verdict file at all is not accepted", accept.decide_acceptance(None, ISSUES)[0], False)
check("garbage is not accepted", accept.decide_acceptance({"items": "nope"}, ISSUES)[0], False)
check("a reject with every item passing is not accepted, and names the findings",
      accept.decide_acceptance(verdict((7, "pass", "ok"), (9, "pass", "ok"), v="reject",
                                       findings=["home page 500s"]), ISSUES),
      (False, "the tester rejected the release: home page 500s", []))
check("issue numbers given as strings still match",
      accept.decide_acceptance({"verdict": "accept", "items": [
          {"issue": "7", "status": "pass", "evidence": "ok"},
          {"issue": "#9", "status": "pass", "evidence": "ok"}]}, ISSUES)[1],
      "the tester did not report on #9")

# ---------- the repair issue

ITEMS = [
    {"issue_number": 7, "issue_title": "Throttle logins", "pr_url": "https://x/pull/70",
     "criteria": json.dumps([
         {"id": "AC1", "mode": "test", "statement": "A sixth attempt is a 429.", "verify": "t.py"},
         {"id": "AC2", "mode": "inspect", "statement": "Docs say so.", "verify": "docs/x.md"}])},
    {"issue_number": 9, "issue_title": "Rate-limit banner", "pr_url": "https://x/pull/90",
     "criteria": json.dumps([
         {"id": "AC1", "mode": "probe", "statement": "Banner renders.", "verify": "curl -fs /"}])},
]
REL = {"id": 4, "repo": "acme/api", "test_sha": "abc123" * 7, "test_url": "https://test-acme.boxd.sh"}
v = verdict((7, "fail", "sixth login returned 200"), (9, "fail", "no banner"), v="reject",
            findings=["/admin is a 500"])
title, body = accept.repair_issue(REL, ITEMS, v, [7, 9])
check("the repair names the release and the failed issues", title,
      "Repair release #4: #7, #9 failed in the browser")
check("the repair passes the dispatch gate by construction", runner.gate_reason(body), None)
cited = runner.parse_criteria(body)
check("it re-cites the failed issues' blocking criteria, ids kept apart per issue",
      [c["id"] for c in cited], ["I7-AC1", "I9-AC1"])
check("inspect criteria are not re-cited", any(c["mode"] == "inspect" for c in cited), False)
check("what the browser saw is in the body", "sixth login returned 200" in body, True)
check("so are the other findings", "/admin is a 500" in body, True)
_, bare = accept.repair_issue(REL, [{"issue_number": 3, "issue_title": "old", "criteria": None}],
                              verdict((3, "fail", "x")), [3])
check("an issue from before criteria were mandatory yields a repair the gate refuses",
      runner.gate_reason(bare) is not None, True)

prompt_items = accept.format_items(ITEMS)
check("the tester is shown each issue's criteria", "I7" not in prompt_items and "A sixth attempt is a 429." in prompt_items, True)
formatted = accept.ACCEPT_PROMPT_TEMPLATE.format(repo="r", release_id=1, sha="s", url="u", items="i")
check("the prompt formats, its JSON example intact", '"verdict": "accept" | "reject"' in formatted, True)

# ---------- Playwright stays where it was put

check("golden-tester is not a golden any build resolves onto", agents.parse_golden(agents.TESTER_SNAPSHOT), None)
check("even when it is the only snapshot there is",
      agents.resolve_snapshot("acme/api", [agents.TESTER_SNAPSHOT]), None)
check("a repo still falls back to golden-copy beside it",
      agents.resolve_snapshot("acme/api", [agents.TESTER_SNAPSHOT, agents.BASE_SNAPSHOT]),
      agents.BASE_SNAPSHOT)
check("no repo slug can be `tester`", agents.golden_name("tester/tester") != agents.TESTER_SNAPSHOT, True)
check("an acceptance VM is a run VM the reconciler reaps", runner.is_run_vm("acc-1234abcd"), True)
check("and the fleet view calls it what it is", runner.vm_role("acc-1234abcd"), "accept")
check("acceptance claims no issue", "accept" in db.UNCLAIMED_KINDS, True)
check("the tester's script never pushes", "git push" in accept.ACCEPT_SCRIPT, False)
check("it checks the release out detached, at its sha",
      'git checkout --detach "$FACTORY_ACCEPT_SHA"' in accept.ACCEPT_SCRIPT, True)
exec_src = inspect.getsource(accept._execute)
check("the acceptance VM boots golden-tester and nothing else",
      "runner._provision(boxd, agents.TESTER_SNAPSHOT" in exec_src and "source_for" not in exec_src, True)
check("and runs under the agent budget like any agent run",
      "runner._stream(" in exec_src and "settings.run_timeout" in exec_src, True)
check("build and review scripts carry no browser install",
      "playwright" in (runner.VM_SCRIPT + runner.REVIEW_SCRIPT).lower(), False)

# ---------- concluding, against the table and a fake GitHub

tmp = tempfile.TemporaryDirectory()
object.__setattr__(settings, "db_path", Path(tmp.name) / "factory.db")
object.__setattr__(settings, "log_dir", Path(tmp.name) / "logs")
settings.log_dir.mkdir(parents=True, exist_ok=True)
object.__setattr__(settings, "accept_max_repairs", 1)
run(db.init())

filed: list[tuple] = []


async def fake_create_issue(repo, title, body, labels=None):
    filed.append((title, tuple(labels or ())))
    return {"number": 200 + len(filed), "html_url": f"https://github.com/{repo}/issues/{200 + len(filed)}"}


github.create_issue = fake_create_issue


def deployed_release():
    async def go():
        r = await db.create_release("acme/api", test_sha="s" * 40)
        await db.add_release_items(r["id"], [{**i, "merged_sha": f"m{i['issue_number']}-{r['id']}"} for i in ITEMS])
        await db.transition_release(r["id"], "promoted", promoted_at=db.utcnow())
        await db.transition_release(r["id"], "deploying")
        await db.transition_release(r["id"], "deployed", test_url=REL["test_url"])
        await db.transition_release(r["id"], "accepting")
        return r["id"]
    return run(go())


log = runner.RunLog(Path(tmp.name) / "accept.log")

rid = deployed_release()
check("a passing verdict accepts the release",
      run(accept.conclude(rid, verdict((7, "pass", "ok"), (9, "pass", "ok")), log)), "accepted")
check("and stamps it", run(db.get_release(rid))["accepted_at"] is not None, True)
run(release.mark_reviewed(rid))

rid = deployed_release()
check("a seen failure rejects the release", run(accept.conclude(rid, v, log)), "rejected")
row = run(db.get_release(rid))
check("filing one queued repair issue", filed,
      [(f"Repair release #{rid}: #7, #9 failed in the browser", ("agent:queued",))])
check("recorded on the release", row["repair_issue"], 201)
now = dt.datetime.now(dt.timezone.utc)
check("a rejected release holds the freeze", release.frozen(row, now, 86400), True)
run(release.mark_reviewed(rid))
row = run(db.get_release(rid))
check("reviewed, it still holds while the repair is open", release.frozen(row, now, 86400, repair_open=True), True)
check("and lets go once the repair has merged", release.frozen(row, now, 86400, repair_open=False), False)

rid = deployed_release()
check("failing again straight after a repair stops for a human", run(accept.conclude(rid, v, log)), "failed")
check("without filing a second repair", len(filed), 1)
check("and says why", "after 1 repair(s) in a row" in run(db.get_release(rid))["error"], True)

rid = deployed_release()
check("an unverifiable verdict fails the release without filing",
      (run(accept.conclude(rid, None, log)), len(filed)), ("failed", 1))


async def closed_issue(repo, number):
    return {"number": number, "state": "closed", "body": "", "title": "", "labels": []}


async def unreadable_issue(repo, number):
    raise RuntimeError("GitHub is down")


github.get_issue = closed_issue
check("a merged repair reads as closed", run(release.repair_open({"repo": "a/b", "repair_issue": 5})), False)
github.get_issue = unreadable_issue
check("a repair GitHub cannot answer about holds the freeze",
      run(release.repair_open({"repo": "a/b", "repair_issue": 5})), True)
check("a release with no repair never asks", run(release.repair_open({"repo": "a/b"})), False)

tick_src = inspect.getsource(release._tick)
check("acceptance is started by the tick only with FACTORY_ACCEPT",
      "if settings.accept_enabled:\n        await accept_deployed()" in tick_src, True)
check("and the schedule consults the open repair before assembling",
      "await repair_open(latest)" in tick_src, True)

print()
print(f"{len(fails)} failed: {', '.join(fails)}" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
