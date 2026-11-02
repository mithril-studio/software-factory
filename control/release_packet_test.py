"""The review packet tells a human the truth about a release, and feedback files only on a click.

The packet is what a person reads instead of the diffs, so the failure to fear is a packet
that looks complete and is not: an item shown beside the verdict of a *different* review of the
same issue (the one that sent it back, say, rather than the one that let it merge), a criterion
the reviewer never reported on shown as if it passed, or a deploy from another release. And
feedback is a note until a human says otherwise — a review form that filed every sentence as
queued work would have the factory building drafts, and a double click that filed two issues
would have it building the same thing twice.

Run it directly, no framework needed:

    .venv/bin/python -m control.release_packet_test
"""
import asyncio
import inspect
import json
import sys
import tempfile
from pathlib import Path

from fastapi import HTTPException

from control import app as app_module
from control import db, github, release
from control.config import settings

fails: list[str] = []


def check(name, got, want=True):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n       got={got!r} want={want!r}"))
    if not ok:
        fails.append(name)


def run(coro):
    return asyncio.run(coro)


tmp = tempfile.TemporaryDirectory()
object.__setattr__(settings, "db_path", Path(tmp.name) / "factory.db")
object.__setattr__(settings, "log_dir", Path(tmp.name) / "logs")
settings.log_dir.mkdir(parents=True, exist_ok=True)
run(db.init())

REPO = "acme/api"


async def seed():
    # Issue 7 was reviewed twice: the first review sent it back, the second approved and merged.
    await db.create_run(
        id="rev-old", repo=REPO, issue_number=7, kind="review", status="succeeded",
        verdict=json.dumps({"verdict": "request_changes", "criteria": [
            {"id": "AC1", "status": "not_met", "evidence": "old evidence"}]}),
        created_at="2026-06-01T10:00:00+00:00",
    )
    await db.create_run(
        id="rev-merge", repo=REPO, issue_number=7, kind="review", status="succeeded",
        merged_sha="m7", created_at="2026-06-01T11:00:00+00:00",
        verdict=json.dumps({"verdict": "approve", "criteria": [
            {"id": "AC1", "status": "met", "evidence": "pytest t.py: 3 passed"},
            {"id": "AC9", "status": "met", "evidence": "a criterion the issue no longer has"}],
            "findings": [], "notes": ["docs untouched"]}),
    )
    # A later review of issue 7 that has nothing to do with this release's merge.
    await db.create_run(
        id="rev-later", repo=REPO, issue_number=7, kind="review", status="succeeded",
        created_at="2026-06-02T09:00:00+00:00",
        verdict=json.dumps({"verdict": "request_changes", "criteria": []}),
    )
    r = await db.create_release(REPO, test_sha="t" * 40)
    await db.add_release_items(r["id"], [{
        "issue_number": 7, "issue_title": "Throttle logins", "pr_url": "https://x/pull/70",
        "merged_sha": "m7", "summary": "Adds a throttle.",
        "criteria": json.dumps([
            {"id": "AC1", "mode": "test", "statement": "A sixth attempt is a 429.", "verify": "t.py"},
            {"id": "AC2", "mode": "inspect", "statement": "Docs say so.", "verify": "docs/x.md"},
        ]),
    }])
    await db.transition_release(r["id"], "promoted", promoted_at=db.utcnow())
    other = await db.create_release("acme/web", test_sha="w" * 40)
    await db.create_run(id="dep-other", repo="acme/web", issue_number=0, kind="deploy",
                        status="succeeded", release_id=other["id"], created_at=db.utcnow())
    await db.create_run(id="dep-1", repo=REPO, issue_number=0, kind="deploy", status="failed",
                        error="first try", release_id=r["id"], created_at="2026-06-02T09:00:00+00:00")
    await db.create_run(id="dep-2", repo=REPO, issue_number=0, kind="deploy", status="succeeded",
                        release_id=r["id"], created_at="2026-06-02T09:05:00+00:00")
    return r["id"]


rid = run(seed())
packet = run(release.packet(rid))
item = packet["items"][0]
check("an item carries the review that merged it, not a later or earlier one",
      item["review"]["run_id"], "rev-merge")
by_id = {c["id"]: c for c in item["criteria"]}
check("each criterion carries the statement the issue had at promotion",
      by_id["AC1"]["statement"], "A sixth attempt is a 429.")
check("beside the reviewer's status and evidence",
      (by_id["AC1"]["status"], by_id["AC1"]["evidence"]), ("met", "pytest t.py: 3 passed"))
check("a criterion the reviewer did not report on says so rather than passing",
      by_id["AC2"]["status"], "not reported")
check("unreported and unmet come before met", item["criteria"][0]["id"], "AC2")
check("what the reviewer reported beyond the issue is still shown", "AC9" in by_id, True)
check("reviewer notes ride along", item["review"]["notes"], ["docs untouched"])
check("the packet shows this release's latest deploy, not another release's",
      packet["deploy"]["id"], "dep-2")
check("no acceptance run yet reads as none", packet["acceptance"], None)

check("packet_criteria survives a verdict that is not a dict",
      [c["status"] for c in release.packet_criteria([{"id": "AC1"}], None)], ["not reported"])

# ---------- feedback: stored on one click, filed on another

filed: list[tuple] = []


async def fake_create_issue(repo, title, body, labels=None):
    filed.append((repo, title, body, tuple(labels or ())))
    if title.startswith("boom"):
        raise RuntimeError("GitHub said no")
    return {"html_url": f"https://github.com/{repo}/issues/{100 + len(filed)}", "number": 100 + len(filed)}


github.create_issue = fake_create_issue
app_module.github = github

GATED = ("The login page double-submits.\n\n## Acceptance criteria\n```yaml\n"
         "- id: AC1\n  mode: test\n  statement: \"one submit\"\n  verify: \"t.py\"\n```\n")

note = run(app_module.api_release_feedback(rid, app_module.Feedback(text="The banner overlaps the menu.")))
check("storing feedback files nothing", filed, [])
check("and warns that, as written, it would not be built", note["gate_reason"] is not None, True)
gated = run(app_module.api_release_feedback(rid, app_module.Feedback(text=GATED)))
check("feedback with a blocking criterion carries no warning", gated["gate_reason"], None)

src = inspect.getsource(app_module.api_release_feedback)
check("the store endpoint never calls create_issue", "create_issue" in src, False)

out = run(app_module.api_file_feedback(rid, gated["id"], app_module.FileFeedback()))
check("filing creates one issue, labelled agent:queued",
      [(f[0], f[3]) for f in filed], [(REPO, ("agent:queued",))])
check("titled by the note's first line", filed[0][1], "The login page double-submits.")
check("with the note verbatim and where it came from",
      GATED.strip() in filed[0][2] and f"release #{rid}" in filed[0][2], True)
check("and the issue is recorded on the note", out["issue_number"], 101)


def status_of(coro):
    try:
        run(coro)
    except HTTPException as exc:
        return exc.status_code
    return 200


check("filing the same note twice is a 409",
      status_of(app_module.api_file_feedback(rid, gated["id"], app_module.FileFeedback())), 409)
check("and opens no second issue", len(filed), 1)

# Found in review: a note without criteria filed `agent:queued` is blocked by the poller on
# the next tick, and a blocked issue halts the repo's whole queue.
plain = run(app_module.api_file_feedback(rid, note["id"], app_module.FileFeedback()))
check("a note without a blocking criterion is filed as a plain issue, not queued",
      filed[-1][3], ())
check("and the response says why", plain["gate_reason"] is not None, True)

boom = run(app_module.api_release_feedback(rid, app_module.Feedback(text="boom: it broke")))
check("GitHub refusing the issue is a 400",
      status_of(app_module.api_file_feedback(rid, boom["id"], app_module.FileFeedback())), 400)
check("and gives the claim back so the note can be filed later",
      run(db.get_release_feedback(boom["id"]))["filed_at"], None)
check("feedback on another release is a 404",
      status_of(app_module.api_file_feedback(rid + 1, note["id"], app_module.FileFeedback())), 404)

packet = run(release.packet(rid))
check("the packet lists the feedback with its filing state",
      [(f["id"], bool(f["filed_at"])) for f in packet["feedback"]],
      [(note["id"], True), (gated["id"], True), (boom["id"], False)])

print()
print(f"{len(fails)} failed: {', '.join(fails)}" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
