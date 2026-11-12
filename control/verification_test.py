"""A builder's success must not hide a missing, failed or stale required browser verdict."""
import copy
import yaml
from . import verification as v

ISSUE = {"number": 1, "title": "lesson", "body": """## Acceptance criteria
```yaml
- id: AC1
  mode: probe
  statement: The lesson awards 10 points.
```
"""}
CONFIG = {"version": 1, "e2e": {"required": True, "setup": "true", "reset": "true",
          "start": "npm start", "cleanup": "true", "ready_url": "http://127.0.0.1:3100",
          "flows": [{"id": "lesson", "start": "/", "task": "Finish a lesson.", "criteria": ["AC1"]}]}}
PROFILE = "## Verification\n```yaml\n" + yaml.safe_dump(CONFIG) + "```\n"
plan = v.freeze(PROFILE, ISSUE)
assert v.freeze("## Setup\ntrue", ISSUE) == {"enabled": False}
assert v.freeze(PROFILE, ISSUE) == plan
for profile, issue in [(PROFILE.replace("version: 1", "version: 2"), ISSUE),
                       (PROFILE.replace("```yaml", "bad"), ISSUE),
                       (PROFILE, {**ISSUE, "body": ISSUE["body"].replace("mode: probe", "mode: typo")}),
                       (PROFILE, {**ISSUE, "body": "no criteria"})]:
    try:
        v.freeze(profile, issue)
        raise AssertionError("invalid contract accepted")
    except ValueError:
        pass
rows = [v.result(plan, "abc", 1, plan["checks"][0], "pass", ["lesson/steps/01.jpg"])]

def action(results=rows, **kw):
    return v.decide(plan, "abc", results, **kw)["action"]

assert action() == "ready_for_review"
assert action([]) == "run_verification"
assert action([], completed=True) == "escalate"
assert action([{**r, "commit": "old"} for r in rows]) == "run_verification"
assert action([{**r, "plan_version": "old"} for r in rows]) == "run_verification"
assert action(rows + rows) == "escalate"
assert action([{**r, "evidence": []} for r in rows]) == "escalate"
assert action([{**r, "verifier": "design"} for r in rows]) == "escalate"
assert action([{**r, "outcome": "fail"} for r in rows]) == "request_fixes"
assert action([{**r, "outcome": "error"} for r in rows]) == "escalate"
assert action([{**r, "outcome": []} for r in rows]) == "escalate"
advisory = copy.deepcopy(plan)
advisory["checks"][0]["required"] = False
assert v.decide(advisory, "abc", [], completed=True)["action"] == "ready_for_review"
raw = {"checks": [{"id": "c1", "verdict": "pass", "steps": [9]}]}
assert action(v.e2e_results(plan, "abc", 1, CONFIG["e2e"]["flows"][0], raw, {1})) == "escalate"
print("verification contract and policy: ALL PASS")
