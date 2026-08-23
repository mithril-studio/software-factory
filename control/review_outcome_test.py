"""What a review run decided, and which of its two counters said so.

Both halves of this file are one bug seen twice on foundation-e-learning#77, whose runs list
read: build **succeeded**, review **changes requested**, build **try 2** — a status going
backwards and a retry of something that had not failed. Neither was true. The reviewer
*approved*; CI then went red on a coverage floor the agent had never been told to run; and the
run that followed was the second pass over the pull request, not the second try at the build.

1. `error` on a review run has three possible authors — a reviewer that refused the change, CI
   rejecting a change the reviewer approved, and a merge that could not happen. The interface
   inferred the first from the column merely being non-empty, so an approved review was
   rendered as a rejection. They are tagged now, and the tags are a contract with a file in
   another language: the checks below are what stops `runner.py` and `web/src/lib/api.ts`
   drifting apart, because nothing else can see both.

2. `attempt` carried both counters. A fix run was created as `attempt=cycle + 1`, so it spent
   the crash-retry budget, reported itself against `max_attempts` when `max_review_cycles`
   governs it, and — worst of the three — arrived at a VM script that resumes the branch only
   when the attempt is past its first. Had the attempt been left at 1 without the rest of this
   change, that script would have reset the branch to the base and discarded the commits the
   reviewer had just approved.

Run it directly, no framework needed:

    .venv/bin/python -m control.review_outcome_test
"""
import inspect
import pathlib
import sys

from control import db, runner

fails: list[str] = []


def check(name, got, want=True):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n       got={got!r} want={want!r}"))
    if not ok:
        fails.append(name)


TAGS = (runner.REVIEW_REFUSED, runner.REVIEW_CI_RED, runner.REVIEW_UNMERGED)


# ---------- the tags are usable as tags

check("the three outcomes are distinct", len(set(TAGS)), 3)
# `startswith` is how both sides read them, so one tag being a prefix of another would make
# the first match win by ordering rather than by meaning.
check("no tag is a prefix of another",
      [a for a in TAGS for b in TAGS if a != b and b.startswith(a)], [])
for tag in TAGS:
    check(f"{tag!r} ends in a separator, so the reason reads as a sentence after it",
          tag.endswith(": "))


# ---------- the server writes all three, and writes nothing else into that column

src = inspect.getsource(runner._execute_review)
for name, tag in (("REVIEW_REFUSED", runner.REVIEW_REFUSED),
                  ("REVIEW_CI_RED", runner.REVIEW_CI_RED),
                  ("REVIEW_UNMERGED", runner.REVIEW_UNMERGED)):
    check(f"the review path tags its {name} outcome", f"{{{name}}}" in src)

# The one that has to stay true for the reading to mean anything: an approved review that
# merged records no reason at all, so `succeeded` with an empty `error` is unambiguous.
check("an approved review clears the column rather than explaining itself",
      "error=None if approved" in src)


# ---------- the interface reads the same three

api = pathlib.Path(__file__).resolve().parent.parent / "web" / "src" / "lib" / "api.ts"
ts = api.read_text(encoding="utf-8")
for tag in TAGS:
    check(f"web/src/lib/api.ts knows {tag!r}", f'"{tag}"' in ts)
# The old rule, which is the bug: any error at all meant the reviewer refused.
check("it no longer decides on the column merely being non-empty",
      'r.status === "succeeded" && r.error) return "changes requested"' in ts, False)


# ---------- attempt and cycle are two numbers

check("a fresh runs table has somewhere to put the cycle", "cycle" in db.SCHEMA)
# And an existing one grows it, because every deployment already has rows.
check("an existing one is migrated to match",
      any("runs ADD COLUMN cycle" in m for m in db.MIGRATIONS))

fix = inspect.getsource(runner._fix_cycle)
check("a fix run is the first attempt of its cycle, not the next attempt of the last one",
      "attempt=1," in fix and "review_cycle=cycle + 1," in fix)
check("and it is still capped by the cycle budget, not the attempt budget",
      "cycle < settings.max_review_cycles" in fix)

fail = inspect.getsource(runner._fail_run)
check("a crash retry stays inside the cycle it crashed in",
      "review_cycle=review_cycle," in fail)

# The VM script resumes a branch when the control plane says there is work on it. Keying that
# on the attempt alone is what would have thrown away an approved change.
check("the build script asks whether to resume, not how many attempts there have been",
      "$FACTORY_RESUME" in runner.VM_SCRIPT and "$FACTORY_ATTEMPT" not in runner.VM_SCRIPT)
execute = inspect.getsource(runner._execute)
check("and either counter being past its first value answers yes",
      "resume=attempt > 1 or review_cycle > 1," in execute)


# ---------- a fix run is told what came back

ISSUE = {"number": 7, "title": "Do the thing", "body": "Please."}


def prompt(**kw) -> str:
    return runner.build_prompt("a/repo", ISSUE, "factory/issue-7", "main", **kw)


plain = prompt()
check("a first run gets no retry or fix context",
      ("retry context" in plain, "fix context" in plain), (False, False))

# The regression this guards: a fix run is attempt 1, so a prompt keyed on `attempt > 1` drops
# the findings and the CI log that are the entire reason the run exists.
fixrun = prompt(attempt=1, review_cycle=2, prior_error="CI failed", prior_log="the log")
check("a fix run is told what came back", "the log" in fixrun and "CI failed" in fixrun)
check("and is told it is a fix, not a repeat of something that failed",
      ("fix cycle 2" in fixrun, "Earlier attempts on this issue failed" in fixrun), (True, False))
check("it is pointed at the commits already on the branch",
      "do not force-push over that work" in fixrun.lower())

retry = prompt(attempt=2, prior_error="crashed", prior_log="the log")
check("a genuine retry still reads as one",
      ("attempt 2 of" in retry, "fix cycle" in retry), (True, False))


print()
if fails:
    print(f"{len(fails)} failed: " + ", ".join(fails))
    sys.exit(1)
print("all passed")
