"""Policy contracts: run with python -m control.verification_test."""
import copy
import json
import tempfile
import asyncio
from pathlib import Path

from control import db, verification as v
from control.config import settings

ISSUE = {"number": 1, "title": "lesson", "body": """## Acceptance criteria
```yaml
- id: AC1
  mode: test
  statement: A completed lesson awards 10 points.
```
"""}
PROFILE = """## Verification
```yaml
version: 1
max_retries: 1
```
"""
plan = v.freeze(PROFILE, ISSUE, 2)
assert v.freeze("## Setup\ntrue", ISSUE, 2) == {"enabled": False}
assert v.freeze(PROFILE, ISSUE, 2) == plan
assert v.freeze(PROFILE.replace("1\n```", "0\n```"), ISSUE, 2)["version"] != plan["version"]
for profile, issue in [(PROFILE.replace("version: 1", "version: 2"), ISSUE),
                       (PROFILE.replace("max_retries: 1", "max_retries: false"), ISSUE),
                       (PROFILE.replace("```yaml", "bad"), ISSUE),
                       (PROFILE, {**ISSUE, "body": ISSUE["body"].replace("mode: test", "mode: typo")}),
                       (PROFILE, {**ISSUE, "body": "no criteria"})]:
    try:
        v.freeze(profile, issue, 2)
        raise AssertionError("invalid contract accepted")
    except ValueError:
        pass

good = {"verdict": "approve", "criteria": [{"id": "AC1", "status": "met", "evidence": "test passed"}]}
rows = v.review_results(plan, "abc", 1, good)


def action(results=rows, **kwargs):
    return v.decide(plan, "abc", results, **kwargs)["action"]


assert action() == "eligible_to_merge"
assert action([]) == "run_verification"
assert action([{**r, "commit": "old"} for r in rows]) == "run_verification"
assert action([{**r, "plan_version": "old"} for r in rows]) == "run_verification"
assert action([], completed=True) == "retry_verification"
assert action([], completed=True, attempt=2) == "escalate"
assert action(rows + [rows[0]]) == "escalate"
assert action([{**r, "evidence": []} for r in rows]) == "escalate"
assert action([{**r, "verifier": "design"} for r in rows]) == "escalate"
failed = [rows[0], {**rows[1], "outcome": "fail"}]
assert action(failed) == "request_fixes"
assert action(failed, cycle=2) == "escalate"
assert action(failed + [{**r, "attempt": 2} for r in rows], attempt=2) == "escalate"
assert action(v.review_results(plan, "abc", 1, good, exit_code=1), completed=True) == "retry_verification"
duplicate = copy.deepcopy(good)
duplicate["criteria"] *= 2
assert action(v.review_results(plan, "abc", 1, duplicate)) == "escalate"
no_evidence = copy.deepcopy(good)
del no_evidence["criteria"][0]["evidence"]
assert action(v.review_results(plan, "abc", 1, no_evidence), completed=True) == "retry_verification"

advisory = copy.deepcopy(plan)
advisory["checks"].append({"id": "design", "verifier": "design", "required": False})
assert v.decide(advisory, "abc", rows)["action"] == "eligible_to_merge"


async def persistence():
    with tempfile.TemporaryDirectory() as tmp:
        old = settings.db_path
        object.__setattr__(settings, "db_path", Path(tmp) / "factory.db")
        try:
            await db.init()
            fields = dict(id="same-decision-fix", repo="a/b", issue_number=1,
                          status="queued", created_at=db.utcnow(), verification_plan=json.dumps(plan))
            assert await db.create_run_once(**fields)
            assert not await db.create_run_once(**fields)
            receipt = {"results": failed, "decision": v.decide(plan, "abc", failed)}
            await db.update_run(fields["id"], verification=json.dumps(receipt))
            await db.init()  # simulate a restart, including migrations
            saved = await db.get_run(fields["id"])
            assert json.loads(saved["verification_plan"]) == plan
            assert json.loads(saved["verification"]) == receipt
            assert not await db.create_run_once(**fields)
        finally:
            object.__setattr__(settings, "db_path", old)


asyncio.run(persistence())
print("verification policy and persistence: ALL PASS")
