"""The fences on a loop that edits its own inputs have to be mechanical, not prompted.

`discussion.md` states the rule this file enforces: things that cost money when ignored go in
flags and harness behaviour, and only things you would *like* go in the prompt. A learning run
is the sharpest case. It reads telemetry and proposes changes to the files that shape how every
later agent behaves, so the two questions that matter — what may it cite, and what may it cause
to be built — cannot be answered by asking it nicely.

So the agent writes a file and the control plane files it. That inversion is what lets these be
checks instead of instructions:

- a citation is verified against runs that exist, so invented evidence cannot enter the ledger;
- `agent:queued` is added here or not at all, so the label that makes the factory build
  something is never in the agent's hands;
- harness proposals are recorded and never queued, so the loop cannot point the factory at the
  control plane that runs it.

Run it directly, no framework needed:

    .venv/bin/python -m control.learn_test
"""
import asyncio
import inspect
import sys
import tempfile
from pathlib import Path

from control import db, poller, runner
from control.config import settings

fails: list[str] = []


def check(name, got, want=True):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n       got={got!r} want={want!r}"))
    if not ok:
        fails.append(name)


tmp = tempfile.TemporaryDirectory()
object.__setattr__(settings, "db_path", Path(tmp.name) / "factory.db")
object.__setattr__(settings, "log_dir", Path(tmp.name) / "logs")
settings.log_dir.mkdir(parents=True, exist_ok=True)


def run(coro):
    return asyncio.run(coro)


run(db.init())

KNOWN = {"r1", "r2", "r3"}


def proposal(**over):
    base = dict(
        artifact="skill", action="add", target=".claude/skills/verify/SKILL.md",
        title="Add a verify-before-PR skill",
        body="Do the thing.\n\n## Acceptance criteria\n- id: x\n  mode: probe",
        rationale="Four runs opened a PR without running verify.",
        evidence={"run_ids": ["r1", "r2"], "signature": "ci red: typecheck"},
        metric="review_rejection_rate", baseline=0.4,
    )
    return {**base, **over}


def valid(items, limit=3):
    return runner.valid_proposals({"proposals": items}, KNOWN, limit)


# ---------- AC1: evidence is checked, not trusted

check("a well-formed proposal survives", len(valid([proposal()])), 1)

# The fence that matters most. An agent reconstructing a run id from memory, or inventing one
# to justify a proposal it liked, produces a ledger row whose evidence leads nowhere — and the
# ledger's only value is that its evidence is real.
check("a proposal citing a run that does not exist is dropped",
      valid([proposal(evidence={"run_ids": ["nope"], "signature": "x"})]), [])
check("a proposal citing nothing at all is dropped",
      valid([proposal(evidence={"run_ids": [], "signature": "x"})]), [])
check("invented citations are stripped, real ones kept",
      __import__("json").loads(
          valid([proposal(evidence={"run_ids": ["r1", "nope"], "signature": "x"})])[0]["evidence"]
      )["run_ids"], ["r1"])

# Falsifiability, same reasoning: a proposal that cannot be graded is one that can never be
# deleted, so it does not get to enter.
check("an unknown metric is dropped", valid([proposal(metric="vibes")]), [])
check("an unknown artifact is dropped", valid([proposal(artifact="prompt")]), [])
check("an unknown action is dropped", valid([proposal(action="tweak")]), [])
check("a proposal with no rationale is dropped", valid([proposal(rationale="  ")]), [])
check("a proposal with no body is dropped", valid([proposal(body="")]), [])

# Total on garbage: this reads a file an agent wrote, so one bad entry must cost that entry
# and nothing else.
check("junk entries are skipped, not fatal", len(valid([None, "x", proposal(), 7])), 1)
check("a missing payload is an empty list, not an error",
      runner.valid_proposals(None, KNOWN, 3), [])
check("a payload with no proposals key is empty",
      runner.valid_proposals({}, KNOWN, 3), [])

# Filtering happens before the cap, so one malformed proposal cannot push a good one out.
mixed = [proposal(metric="vibes"), proposal(title="A"), proposal(title="B")]
check("the cap applies to what survived, not to what was submitted",
      [p["title"] for p in valid(mixed, limit=2)], ["A", "B"])
check("and the cap is enforced",
      len(valid([proposal(title=f"T{i}") for i in range(9)], limit=3)), 3)


# ---------- AC2: what the agent may cause to be built

src = inspect.getsource(runner._file_proposals)

# The destination is not the agent's to choose. Every issue goes to the repo the run was
# dispatched for, so a proposal cannot reach another repository or this control plane.
check("issues are filed against the run's own repo",
      "github.create_issue(\n                repo," in src or "create_issue(repo," in src, True)
check("the label is decided here, not taken from the proposal",
      "settings.learn_autoqueue" in src and "LABEL_QUEUED" in src, True)

# Two artifacts are recorded but never built, because what they change is not in the repo
# being learned about: `harness` is this control plane, `compose` is the skill that writes
# the work orders. Queuing either would point the factory at itself or at its own
# instructions.
check("unbuildable artifacts are never queued whatever the setting says",
      "db.IMPROVEMENT_UNBUILDABLE" in src, True)
check("and the harness is one of them", "harness" in db.IMPROVEMENT_UNBUILDABLE, True)
check("as is the work order itself", "compose" in db.IMPROVEMENT_UNBUILDABLE, True)
check("but a repo skill is buildable", "skill" in db.IMPROVEMENT_UNBUILDABLE, False)
check("every unbuildable artifact is a real artifact",
      db.IMPROVEMENT_UNBUILDABLE <= db.IMPROVEMENT_ARTIFACTS, True)

# A `compose` proposal is a first-class outcome, not a fallback. An agent can only be as good
# as what it was asked for, and the same rejection read as an agent defect produces a skill
# teaching every future builder to compensate for a badly-written issue — context loaded on
# runs that never needed it, paid for forever, while the issues stay wrong.
check("a proposal blaming the work order is accepted",
      len(valid([proposal(artifact="compose", action="edit",
                          target="factory-compose")])), 1)
learn_prompt = runner.LEARN_PROMPT_TEMPLATE
check("the prompt asks whether the issue was the problem before anything else",
      learn_prompt.index("was the issue the problem?")
      < learn_prompt.index("Is it a fact about this repo?"), True)
check("and tells the analyst to actually read the issues",
      "gh issue view" in learn_prompt, True)

# The silent failure this guards. Claude Code resolves same-named skills personal-over-project
# — the opposite of most-specific-wins — and a golden installs the shared skills personally.
# So a repo skill reusing a global name is never loaded rather than loudly overridden: it
# merges, sits unread, reads as zero-loads to the eviction query, and gets proposed for
# deletion. Every step looks correct, which is why it has to be prompted against explicitly.
check("the analyst is told to check for a shadowing global skill",
      "ls ~/.claude/skills" in learn_prompt, True)
check("and to read a shadowed skill as a rename rather than a delete",
      "rename it rather than to delete it" in learn_prompt, True)
check("the eviction query warns its caller about the same trap",
      "shadowed" in inspect.getsource(
          __import__("telemetry.store", fromlist=["skill_loads_by_repo"]).skill_loads_by_repo
      ), True)

# The env var is only set on the path that reads it back.
env = runner.dispatch_env(repo="a/b", branch="main", base="main", prompt="p",
                          run_id="x", number=0, vm_name="vm", kind="learn")
check("a learning run is not told to file memory candidates",
      runner.MEMORY_CANDIDATE_ENV in env, False)
check("but a build still is",
      runner.MEMORY_CANDIDATE_ENV in runner.dispatch_env(
          repo="a/b", branch="b", base="main", prompt="p", run_id="x",
          number=1, vm_name="vm", kind="build"), True)
check("and the trace says which kind this was", "kind=learn" in env["OTEL_RESOURCE_ATTRIBUTES"], True)


# ---------- AC3: the trigger counts evidence, not time

async def seed(rows):
    for row in rows:
        await db.create_run(**row)

REPO = "acme/web"
run(seed([
    dict(id=f"b{i}", repo=REPO, issue_number=i, status="succeeded", kind="build",
         created_at=db.utcnow()) for i in range(1, 4)
]))
check("finished issues are counted", run(db.issues_since_last_learn(REPO)), 3)

# Four dispatches at one problem is one piece of evidence about that problem. Counting runs
# instead of issues would make the flakiest repo learn most often on the least new information.
run(seed([dict(id=f"retry{i}", repo=REPO, issue_number=3, status="failed", kind="build",
                attempt=i, created_at=db.utcnow()) for i in (2, 3)]))
check("retries of one issue still count once", run(db.issues_since_last_learn(REPO)), 3)

# Reviews and CI are phases of an issue, not issues.
run(seed([dict(id="rev1", repo=REPO, issue_number=1, status="succeeded", kind="review",
               created_at=db.utcnow())]))
check("other phases are not evidence of their own", run(db.issues_since_last_learn(REPO)), 3)

run(seed([dict(id="other", repo="acme/api", issue_number=9, status="succeeded", kind="build",
               created_at=db.utcnow())]))
check("another repo's work does not trigger this one",
      run(db.issues_since_last_learn(REPO)), 3)

# After a learning run the count restarts, so the next one needs genuinely new work rather
# than re-reading the window it just finished with.
#
# Explicit timestamps rather than sleeping between writes: `utcnow()` has second granularity,
# so rows created in the same second are indistinguishable to a `>` comparison and the only
# alternative would be to make the test wait out a real second twice.
LATER = "2099-01-01T00:00:00+00:00"
LATER_STILL = "2099-01-02T00:00:00+00:00"
run(seed([dict(id="l1", repo=REPO, issue_number=0, status="succeeded", kind="learn",
               created_at=LATER)]))
check("the counter resets once a learning run happens",
      run(db.issues_since_last_learn(REPO)), 0)
run(seed([dict(id="b9", repo=REPO, issue_number=9, status="succeeded", kind="build",
               created_at=LATER_STILL)]))
check("and counts only what happened after it", run(db.issues_since_last_learn(REPO)), 1)

# Off by default: this is the one part of the system that edits its own inputs.
# What a proposal may cite, as a query rather than a filter over the capped, fleet-wide
# `list_runs`. Past that cap this repo's older in-window runs fell off the end, so valid
# citations would start being rejected as invented — silently, and more often the busier the
# factory got, which is the worst possible direction for a check on evidence.
cited = run(db.run_ids_since(REPO, "2000-01-01T00:00:00+00:00"))
check("every one of the repo's runs is citable", {"b1", "b2", "b3"} <= cited, True)
check("another repo's runs are not", "other" in cited, False)
check("nor anything before the window starts",
      run(db.run_ids_since(REPO, LATER_STILL)), {"b9"})
check("and a window past everything cites nothing at all",
      run(db.run_ids_since(REPO, "2099-06-01T00:00:00+00:00")), set())

check("the loop ships switched off", settings.learn_enabled, False)
check("and does not queue what it files until told to", settings.learn_autoqueue, False)
poll_src = inspect.getsource(poller._maybe_learn)
check("the kill switch is checked before anything is dispatched",
      poll_src.index("learn_enabled") < poll_src.index("create_learn"), True)
# A learning run competes for a VM with the work it exists to improve, so it waits until
# there is none.
check("learning only starts when nothing is queued",
      "_maybe_learn(repo)" in inspect.getsource(poller._poll_repo), True)


# ---------- AC4: a proposal has to be able to reach `merged`, or nothing is ever graded

# The hole this closes: the ledger defines `proposed → building → merged → kept/reverted`, and
# a state machine nothing drives is a table that fills up with `proposed` and never grades a
# single change. The link is the issue number the proposal became.

LOG = runner.RunLog(settings.log_dir / "advance.log")

run(db.create_improvement(
    id="imp_live", repo=REPO, run_id="learn-1", artifact="skill", action="add",
    rationale="because", evidence='{"run_ids": ["b1"]}', metric="ship_nothing_rate",
    baseline=0.5, issue_url=f"https://github.com/{REPO}/issues/42", issue_number=42,
))

check("the proposal is found by the issue it became",
      run(db.improvement_for_issue(REPO, 42))["id"], "imp_live")
check("an ordinary issue has no proposal behind it",
      run(db.improvement_for_issue(REPO, 999)), None)
check("and neither does another repo's issue with the same number",
      run(db.improvement_for_issue("acme/api", 42)), None)

run(runner.advance_improvement(REPO, 42, "building", LOG))
check("the factory picking it up advances it",
      run(db.get_improvement("imp_live"))["status"], "building")

run(runner.advance_improvement(REPO, 42, "merged", LOG, pr_url="https://gh/pull/7"))
merged = run(db.get_improvement("imp_live"))
check("merging it advances it again", merged["status"], "merged")
check("and records the PR that did", merged["pr_url"], "https://gh/pull/7")
# Which is the state `_grade` looks for. Without these two transitions the grader's input set
# is empty forever and every change the loop makes is permanent by default.
check("merged rows are what the grader reads",
      "status\") == \"merged\"" in inspect.getsource(runner._grade), True)

# Bookkeeping must never fail a run, and an issue nobody proposed is the overwhelmingly
# common case rather than an error.
run(runner.advance_improvement(REPO, 999, "merged", LOG))
run(runner.advance_improvement(REPO, 42, "building", LOG))
check("an impossible transition is logged, not raised",
      run(db.get_improvement("imp_live"))["status"], "merged")

create_src = inspect.getsource(runner.create)

# Advancing at dispatch must not open a file. `create` runs for every build the factory
# dispatches, and it opened a `RunLog` there purely to report a transition that almost never
# happens — one descriptor leaked per build, on the hottest path in the system.
check("dispatch does not open a run log to say nothing",
      "RunLog(" in create_src, False)
check("advance_improvement works without one",
      inspect.signature(runner.advance_improvement).parameters["log"].default, None)

# A retry is the same proposal still being built, and `building → building` is not an edge.
check("only a first attempt advances a proposal",
      "attempt == 1 and review_cycle == 1" in create_src, True)
# Recorded where the factory learns a merge happened, so both callers of `_merge` get it.
check("the merge transition lives in _merge, not in one of its callers",
      '"merged"' in inspect.getsource(runner._merge), True)


tmp.cleanup()
print()
print(f"{len(fails)} failed: {', '.join(fails)}" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
