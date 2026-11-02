"""A fix run must be told what the reviewer ran, not only what it concluded.

The review records, per criterion, a status and the evidence behind it — a command and its
output, a test name, a `file:line`. The fix run it sent back was handed none of that: only the
findings list, often "AC2 not_met" and nothing else. So every fix cycle opened by re-deriving
which command had failed and why, spending turns to reach a fact the reviewer had already
written down, and sometimes fixing a different reading of the criterion than the one checked.

Run it directly, no framework needed:

    .venv/bin/python -m control.fix_evidence_test
"""
import inspect
import sys

from control import runner

fails: list[str] = []


def check(name, got, want=True):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n       got={got!r} want={want!r}"))
    if not ok:
        fails.append(name)


CRITERIA = [
    {"id": "AC1", "mode": "test", "statement": "A sixth attempt is rejected with 429.",
     "verify": "tests/throttle.test.ts"},
    {"id": "AC2", "mode": "structure", "statement": "No handler reads the table directly.",
     "verify": "! grep -rn login_attempts src/app/"},
    {"id": "AC3", "mode": "inspect", "statement": "Docs record the thresholds.",
     "verify": "docs/security.md"},
]
VERDICT = {
    "verdict": "request_changes",
    "criteria": [
        {"id": "AC1", "status": "met", "evidence": "npx vitest tests/throttle.test.ts: 3 passed"},
        {"id": "AC2", "status": "not_met",
         "evidence": "grep printed src/app/login.ts:41: db.login_attempts.findMany " + "x" * 2000},
    ],
    "findings": ["src/app/login.ts reads login_attempts directly"],
    "notes": ["docs/security.md is untouched"],
}

detail = runner.review_detail(VERDICT, CRITERIA, VERDICT["findings"])
print(detail)
print()

check("an unmet criterion carries its statement",
      "No handler reads the table directly." in detail, True)
check("an unmet criterion carries the command it is verified by",
      "! grep -rn login_attempts src/app/" in detail, True)
check("an unmet criterion carries the reviewer's evidence",
      "src/app/login.ts:41" in detail, True)
check("evidence is bounded", "x" * (runner.EVIDENCE_MAX + 1) in detail, False)
check("a criterion the reviewer never mentioned says so",
      "AC3 [inspect] not reported" in detail, True)
check("unmet criteria are listed before met ones",
      detail.find("AC2") < detail.find("AC1"), True)
check("a met criterion is still listed, as something to keep true",
      "AC1 [test] met" in detail, True)
check("findings ride along", "reads login_attempts directly" in detail, True)
check("notes ride along", "docs/security.md is untouched" in detail, True)

check("no verdict at all still names every criterion",
      all(c["id"] in runner.review_detail(None, CRITERIA, []) for c in CRITERIA), True)
check("garbage in the verdict degrades rather than raising",
      "not reported" in runner.review_detail(
          {"criteria": ["AC1", None, {"status": "met"}], "notes": None}, CRITERIA, []), True)

# ---------- wiring

review_src = inspect.getsource(runner._execute_review)
check("the review path hands review_detail to the fix cycle",
      "detail=review_detail(verdict, criteria, findings)" in review_src, True)
check("the issue comment still carries only the short findings list",
      "going_back=f\"Review {cycle} requested changes ({why}):\\n{detail}" in review_src, True)
check("the fix header tells the agent to re-run the evidence",
      "re-run that command" in runner.FIX_HEADER, True)

print()
print(f"{len(fails)} failed: {', '.join(fails)}" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
