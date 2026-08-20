"""Starting a run by hand, and the one thing that made `agent:blocked` a dead end.

The automatic paths already carry *why* a run is happening into the next prompt: a retry after
a failure and a fix run after a red CI or a reviewer that requested changes both call
`runner.create` with `prior_error` and `prior_log` (`runner._fail_run`, `runner._fix_cycle`).
`POST /api/runs` did not, so the only way to resume a blocked issue was to dispatch an agent
onto the branch with "reason not captured" in place of the reviewer's finding — which is not a
resume, it is the same run again. Issue #51 sat blocked behind exactly that, halting every
issue after it, with a green pull request and a two-line change outstanding.

So this pins the wiring rather than the plumbing: nothing here starts a VM, talks to GitHub or
opens a database. `runner.create` is replaced with a recorder, and what is under test is which
arguments reach it.

Run it directly, no framework needed:

    .venv/bin/python -m control.start_run_test
"""
import asyncio
import sys

from control import app as app_module
from control import runner
from control.app import StartRun, api_start_run
from control.config import settings

fails = []


def check(name, got, want=True):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n       got={got!r} want={want!r}"))
    if not ok:
        fails.append(name)


# `missing()` gates the endpoint and reads real credentials; this test is about argument
# passing, so give it the two it looks for rather than the whole environment.
object.__setattr__(settings, "boxd_api_key", "test-key")
object.__setattr__(settings, "github_token", "test-token")

seen = {}


async def fake_create(repo, number, **kwargs):
    seen.clear()
    seen.update({"repo": repo, "number": number, **kwargs})
    return "run-1"


runner.create = fake_create
app_module.runner = runner


def start(**fields):
    seen.clear()
    body = StartRun(repo="acme/api", issue_number=51, kind="build", **fields)
    return asyncio.run(api_start_run(body))


# ---------- a plain dispatch is unchanged

start()
check("a first attempt carries no prior context", seen.get("attempt"), 1)
check("and does not invent a reason", seen.get("prior_error"), None)
check("nor a log", seen.get("prior_log"), None)

# ---------- resuming a blocked issue

start(attempt=3, prior_error="reviewer requested changes: mislabelled domains",
      prior_log="control/runner.py:344 stamps every row with domains[0]")
check("a resume says which attempt it is, so VM_SCRIPT keeps the branch",
      seen.get("attempt"), 3)
check("the reason reaches the agent instead of 'reason not captured'",
      seen.get("prior_error"), "reviewer requested changes: mislabelled domains")
check("and so does the detail it has to act on",
      seen.get("prior_log"), "control/runner.py:344 stamps every row with domains[0]")

# The two fields are independent: a human who knows the reason but has no log to hand should
# not have to invent one to be heard.
start(attempt=2, prior_error="do it the other way")
check("a reason with no log is accepted", seen.get("prior_error"), "do it the other way")
check("and the log stays absent rather than becoming a string", seen.get("prior_log"), None)


# ---------- the fields belong to a build

# A review counts cycles, not attempts, and takes its context from the pull request it is
# reviewing. Passing prior context to one is meaningless rather than harmful, so it is simply
# not forwarded — this pins that it is not quietly accepted and dropped somewhere later.
check("review runs still need a pr_url and a branch",
      "pr_url" in StartRun.model_fields and "branch" in StartRun.model_fields, True)
check("prior context is optional, so every existing caller keeps working",
      (StartRun(repo="acme/api", issue_number=1).prior_error,
       StartRun(repo="acme/api", issue_number=1).prior_log), (None, None))


print()
print(f"{len(fails)} failed" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
