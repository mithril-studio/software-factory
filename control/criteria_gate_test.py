"""An issue with nothing a reviewer can run must never reach the base branch.

Two paths let one through. The poller dispatched any `agent:queued` issue, criteria or not, so
a VM and forty minutes were spent building something whose only safe ending was a human. And
the build path, finding no criteria, skipped the review and fell through to auto-merge — so
with `FACTORY_AUTO_MERGE=1` an issue without criteria merged on the builder's word alone, the
one outcome the review pipeline was built to make impossible.

So: `gate_reason` decides, the poller asks it before any VM exists, and the build path stops
for a human instead of merging when an issue arrives without criteria by hand.

Run it directly, no framework needed:

    .venv/bin/python -m control.criteria_gate_test
"""
import asyncio
import inspect
import sys

from control import poller, runner
from control.config import settings

fails: list[str] = []


def check(name, got, want=True):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n       got={got!r} want={want!r}"))
    if not ok:
        fails.append(name)


def block(yaml_text: str) -> str:
    return f"## Task\nDo it.\n\n## Acceptance criteria\n```yaml\n{yaml_text}```\n"


GATED = block(
    "- id: AC1\n  mode: test\n  statement: \"x\"\n  verify: \"t.py\"\n"
    "- id: AC2\n  mode: inspect\n  statement: \"y\"\n  verify: \"README.md\"\n"
)
INSPECT_ONLY = block("- id: AC1\n  mode: inspect\n  statement: \"y\"\n  verify: \"README.md\"\n")
BROKEN = block("- id: AC1\n  mode: test\n  statement: \"a: b\" oops {\n")
PROSE = "## Acceptance criteria\n- [ ] it works\n"

# ---------- the rule

check("an issue with a blocking criterion may be dispatched", runner.gate_reason(GATED), None)
for mode in ("probe", "structure"):
    check(f"a `{mode}` criterion is blocking too",
          runner.gate_reason(block(f"- id: AC1\n  mode: {mode}\n  statement: \"x\"\n")), None)
check("no block at all is refused, and says so",
      "no `## Acceptance criteria` block" in (runner.gate_reason("## Task\nprose") or ""), True)
check("an empty body is refused", runner.gate_reason("") is not None, True)
check("a prose checklist under the heading is refused as having no block",
      "no `## Acceptance criteria` block" in (runner.gate_reason(PROSE) or ""), True)
check("a block that does not parse is refused, and says so",
      "does not parse" in (runner.gate_reason(BROKEN) or ""), True)
check("inspect-only criteria are refused, because inspect never blocks",
      "inspect" in (runner.gate_reason(INSPECT_ONLY) or ""), True)
check("the format the comment names lists every blocking mode",
      all(m in runner.CRITERIA_FORMAT for m in runner.BLOCKING_MODES), True)

# ---------- the poller asks before any VM exists

calls: list[tuple] = []
bodies = {1: PROSE, 2: GATED}


async def fake_active(repo):
    return False


async def fake_list(repo, label):
    calls.append(("list", label))
    return [{"number": n, "title": ""} for n in sorted(bodies)] if label == "agent:queued" else []


async def fake_get(repo, number):
    return {"number": number, "title": "", "body": bodies[number], "labels": []}


async def fake_remove(repo, number, label):
    calls.append(("remove", number, label))


async def fake_add(repo, number, labels):
    calls.append(("add", number, tuple(labels)))


async def fake_comment(repo, number, body):
    calls.append(("comment", number, body))


async def fake_create(repo, number, **kwargs):
    calls.append(("create", number))
    return "run"


async def noop(*a, **k):
    return None


poller.db.has_active_run = fake_active
poller.github.list_issues_with_label = fake_list
poller.github.get_issue = fake_get
poller.github.remove_label = fake_remove
poller.github.add_labels = fake_add
poller.github.add_comment = fake_comment
poller.runner.create = fake_create
poller._labelled.add("acme/api")
object.__setattr__(settings, "require_criteria", True)
object.__setattr__(settings, "halt_on_failure", True)

asyncio.run(poller._poll_repo("acme/api"))
check("an issue without criteria is not dispatched", ("create", 1) in calls, False)
check("it loses agent:queued", ("remove", 1, "agent:queued") in calls, True)
check("it gains agent:blocked", ("add", 1, ("agent:blocked",)) in calls, True)
comment = next((c[2] for c in calls if c[0] == "comment"), "")
check("its comment names the format to add", "factory-compose" in comment, True)
check("nothing else is dispatched in the same tick", any(c[0] == "create" for c in calls), False)

calls.clear()
bodies.pop(1)
asyncio.run(poller._poll_repo("acme/api"))
check("an issue with criteria is dispatched", ("create", 2) in calls, True)
check("and is not labelled blocked", any(c[0] == "add" for c in calls), False)

calls.clear()
bodies[1] = PROSE
object.__setattr__(settings, "require_criteria", False)
asyncio.run(poller._poll_repo("acme/api"))
check("FACTORY_REQUIRE_CRITERIA=0 dispatches the lowest issue as before", ("create", 1) in calls, True)
object.__setattr__(settings, "require_criteria", True)

poll_src = inspect.getsource(poller._poll_repo)
check("the gate sits before the dispatch in _poll_repo",
      0 < poll_src.find("_refuse_ungated") < poll_src.find("runner.create("), True)

# ---------- the build path never merges an issue without criteria

execute_src = inspect.getsource(runner._execute)
check("the build path decides reviewability with gate_reason",
      "unreviewable = gate_reason(" in execute_src, True)
check("an unreviewable PR is caught before the auto-merge branch",
      execute_src.find("elif unreviewable:") < execute_src.find("elif settings.auto_merge:"), True)
check("the old fall-through that merged without criteria is gone",
      "issue carries no acceptance criteria; skipping review" in execute_src, False)
check("an unreviewable PR stops at agent:blocked where it would have auto-merged",
      "if settings.auto_merge:\n                    label = github.LABEL_BLOCKED" in execute_src, True)

print()
print(f"{len(fails)} failed: {', '.join(fails)}" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
