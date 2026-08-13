"""The two deterministic pieces of review: reading an issue's criteria, and turning a
reviewer's verdict into a merge decision.

Both must fail closed, which is the whole point — everything else in a review run is an agent
exercising judgement, and these are the two places that decide what its judgement is allowed
to do. Every case below is a way an agent could wave a bad pull request through.

Run it directly, no framework needed:

    .venv/bin/python -m control.review_logic_test
"""
import sys

from control.runner import parse_criteria, decide

ISSUE = """<!-- factory-compose: demo step 3/7 -->

## Context
Whatever.

## Acceptance criteria
```yaml
- id: AC1
  mode: test
  statement: "A sixth attempt within 15 minutes is rejected with 429."
  verify: "tests/integration/throttle.test.ts"
- id: AC2
  mode: structure
  statement: "No handler reads the table directly."
  verify: "! grep -rn 'login_attempts' src/app/"
- id: AC3
  mode: inspect
  statement: "docs/security.md records the thresholds."
  verify: "docs/security.md"
```

## Out of scope
- Nothing.
"""

fails = []
def check(name, got, want):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}\n       got={got!r}")
    if not ok:
        fails.append(name)

# ---------- parsing
c = parse_criteria(ISSUE)
check("parses 3 criteria", [x["id"] for x in c], ["AC1", "AC2", "AC3"])
check("no criteria block -> []", parse_criteria("## Task\njust prose"), [])
check("prose checklist (every old issue) -> []",
      parse_criteria("## Acceptance criteria\n- [ ] it works\n"), [])
check("malformed yaml -> []",
      parse_criteria('## Acceptance criteria\n```yaml\n- id: AC1\n  mode: test\n  statement: "a: b" oops {\n```\n'), [])
check("unknown mode dropped",
      [x["id"] for x in parse_criteria('## Acceptance criteria\n```yaml\n- id: AC1\n  mode: vibes\n  statement: "x"\n- id: AC2\n  mode: test\n  statement: "y"\n```\n')],
      ["AC2"])

# ---------- deciding
blocking_ok = [{"id": "AC1", "status": "met"}, {"id": "AC2", "status": "met"},
               {"id": "AC3", "status": "cannot_verify"}]

check("all blocking met + approve -> merge",
      decide({"verdict": "approve", "criteria": blocking_ok}, c)[0], True)

check("advisory AC3 unmet does not block",
      decide({"verdict": "approve", "criteria": [
          {"id": "AC1", "status": "met"}, {"id": "AC2", "status": "met"},
          {"id": "AC3", "status": "not_met"}]}, c)[0], True)

check("agent says approve but AC2 not_met -> BLOCKED",
      decide({"verdict": "approve", "criteria": [
          {"id": "AC1", "status": "met"}, {"id": "AC2", "status": "not_met"}]}, c)[0], False)

check("cannot_verify on a blocking criterion -> BLOCKED",
      decide({"verdict": "approve", "criteria": [
          {"id": "AC1", "status": "cannot_verify"}, {"id": "AC2", "status": "met"}]}, c)[0], False)

check("silently omitting a criterion -> BLOCKED",
      decide({"verdict": "approve", "criteria": [{"id": "AC1", "status": "met"}]}, c)[0], False)

check("no verdict file at all -> BLOCKED", decide(None, c)[0], False)
check("garbage verdict -> BLOCKED", decide({"nonsense": 1}, c)[0], False)
check("empty criteria list reported -> BLOCKED", decide({"verdict": "approve", "criteria": []}, c)[0], False)

check("reviewer may still block for its own reasons",
      decide({"verdict": "request_changes", "criteria": blocking_ok,
              "findings": ["leaks a secret into the log"]}, c)[0], False)

check("findings are carried through",
      decide({"verdict": "request_changes", "criteria": blocking_ok,
              "findings": ["a", "b"]}, c)[2], ["a", "b"])

print()
print(f"{len(fails)} failed" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
