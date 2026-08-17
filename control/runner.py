"""Fork a VM, run an agent in it, collect the result, reap the VM.

This module is the whole factory. It contains no model call: it decides nothing about the
work itself, only about machine lifecycle. All intelligence is the agent inside the VM.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import uuid
from pathlib import Path

import yaml
from boxd import AsyncBoxd

from telemetry.recorder import Recorder

from . import db, github
from .config import settings

# The coding agent that runs inside the VM. A constant for now — there is exactly one. It
# becomes meaningful once a review/PR agent joins and runs need to say which one produced them.
AGENT = "claude-code"

# Run ids of in-flight runs -> their asyncio task, so the UI can cancel them.
_tasks: dict[str, asyncio.Task] = {}
_semaphore: asyncio.Semaphore | None = None


def semaphore() -> asyncio.Semaphore:
    """Created lazily so it binds to the running loop, not import-time."""
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(settings.max_concurrent)
    return _semaphore


def client() -> AsyncBoxd:
    return AsyncBoxd(api_key=settings.boxd_api_key)


async def golden_id(boxd: AsyncBoxd, golden: str) -> str:
    """Resolve the golden's machine id.

    The boxd SDK forks by machine **id**, not name — forking by name fails with
    `source VM not found` even though the machine lists fine. `FACTORY_GOLDEN` is a
    human-readable name, so resolve it here. Accepts an id too, so either works in config.
    """
    for m in await boxd.machines.list():
        if m.id == golden or m.name == golden:
            return m.id
    raise RuntimeError(f"golden VM {golden!r} not found in the boxd fleet")


# --------------------------------------------------------------------------- prompt


PROMPT_TEMPLATE = """You are working autonomously in an isolated VM on a checked-out git \
repository. Resolve the GitHub issue below, then open a pull request.

Repository: {repo}
Issue #{number}: {title}

--- issue body ---
{body}
--- end issue body ---

How to work:
1. Load the `memory` skill first and prime yourself from `.mem/` if it exists. What past
   runs learned about this repo is the most valuable context you have.
2. Make the change. Stay in scope: resolve this issue, nothing more.
   If the issue carries an `## Acceptance criteria` block, that is the contract. Every
   criterion must be true when you are done, and a reviewing agent will afterwards run each
   one rather than take your word for it. For `mode: test` criteria, write the test at the
   path given in `verify`, and make sure it **fails before your change and passes after** —
   a test that passes either way proves nothing and will be rejected.
3. Commit and push as you go — after each meaningful step, not once at the end:
   git add -A && git commit -m "<message>" && git push -u origin {branch}
   The branch is what survives; this VM is destroyed when you exit. If this run dies
   half-way, whatever you pushed is what the next attempt continues from, so small
   commits that build on each other are worth far more than one perfect commit you
   never got to make.
4. Verify with the repo's fast checks, and only these:
   npm run lint · npm run typecheck · npm run test · npm run test:integration · npm run build
   Do NOT run the end-to-end suite (`npm run test:e2e`) and do NOT install browsers —
   CI runs end-to-end on your pull request, it is not your job here. Do not add a test
   framework or test runner that isn't already in the repo.
5. Record anything durable you learned into `.mem/`, following the memory skill.
6. Push the final commit and open a pull request with `gh pr create --fill --base {base}`,
   referencing the issue in the body so it links (e.g. "Closes #{number}").

Environment notes — this machine is already set up, so setup work is wasted work:
- Dependencies are installed and the build cache is warm. Do not run `npm ci`, do not
  delete `node_modules`, and do not clear the build output. Install a package only if the
  issue genuinely needs a new one.
- `.env` is correct and read-only. Do not create, copy or modify it, and do not print its
  contents.
- The test database is already running, migrated and seeded. Do not start, reset or
  re-seed it.
- Run long commands in the foreground with an explicit timeout, e.g.
  `timeout 600 npm run build`. Do not put work in the background to wait for it later:
  background tasks and scheduled wake-ups do not deliver notifications in this
  environment, so a run that waits for one waits forever and dies having produced nothing.

If you cannot resolve the issue, still push what you have and open a draft PR explaining
what blocked you. A run that ends with no PR gives the human nothing to look at.
"""


# --------------------------------------------------------------------------- criteria

# The acceptance-criteria block an issue carries, per the factory-compose issue template: a
# fenced yaml list under an "## Acceptance criteria" heading.
_CRITERIA_BLOCK = re.compile(
    r"^##\s*Acceptance criteria\s*?\n+```ya?ml\n(.*?)^```", re.S | re.M | re.I
)

# Modes whose verdict may block a merge. `inspect` needs human judgement, so it is reported
# and never blocks — an agent's opinion is not a gate.
BLOCKING_MODES = ("test", "probe", "structure")


def parse_criteria(body: str) -> list[dict]:
    """Extract an issue's acceptance criteria. Returns [] when there are none to run.

    Deliberately done here rather than by the reviewing agent: what the criteria *are* is not
    a judgement call, and an agent that reads its own contract can also misread it. A malformed
    block yields [] — which skips review rather than inventing criteria, so the failure mode is
    "unreviewed like before", never "reviewed against something nobody wrote".
    """
    match = _CRITERIA_BLOCK.search(body or "")
    if not match:
        return []
    try:
        parsed = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return []
    if not isinstance(parsed, list):
        return []
    out = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        if not {"id", "mode", "statement"} <= set(item):
            continue
        if item["mode"] not in (*BLOCKING_MODES, "inspect"):
            continue
        out.append(item)
    return out


def _run_link(run_id: str) -> str | None:
    return f"{settings.base_url}/runs/{run_id}" if settings.base_url else None


async def _mirror_issue(
    repo: str,
    number: int,
    add: str | None,
    remove: list[str],
    log: RunLog,
    comment: str | None = None,
) -> None:
    """Reflect run state onto the issue as a label, optionally leaving a comment.

    Labels mirror the runs table for humans reading GitHub; they are never read back as
    truth. So this is best-effort by design — a GitHub hiccup is logged and swallowed, never
    allowed to fail an otherwise good run.
    """
    try:
        for label in remove:
            await github.remove_label(repo, number, label)
        if add:
            await github.add_labels(repo, number, [add])
        if comment:
            await github.add_comment(repo, number, comment)
    except Exception as exc:  # noqa: BLE001
        log.write(f"[factory] issue update skipped: {exc!r}")


RETRY_TEMPLATE = """

--- retry context ---
This is attempt {attempt} of {max_attempts}. Earlier attempts on this issue failed
({prior_error}). Below is the tail of the previous attempt's log. Read it, work out the root
cause, and fix *that* — do not blindly repeat what failed.

You are already checked out on {branch}, including any commits an earlier attempt pushed to
it — read them first (`git log --oneline origin/{base}..HEAD` and `git diff origin/{base}`)
and continue from there. Do not start over, and do not force-push over that work.

previous attempt log (tail):
{prior_log}
--- end retry context ---
"""


def build_prompt(
    repo: str,
    issue: dict,
    branch: str,
    base: str,
    attempt: int = 1,
    prior_error: str | None = None,
    prior_log: str | None = None,
) -> str:
    prompt = PROMPT_TEMPLATE.format(
        repo=repo,
        number=issue["number"],
        title=issue["title"],
        body=issue["body"] or "(no description given)",
        branch=branch,
        base=base,
    )
    if attempt > 1:
        prompt += RETRY_TEMPLATE.format(
            attempt=attempt,
            max_attempts=settings.max_attempts,
            prior_error=prior_error or "reason not captured",
            prior_log=prior_log or "(previous log unavailable)",
            branch=branch,
            base=base,
        )
    return prompt


def _log_tail(run_id: str, max_chars: int = 4000) -> str:
    """The tail of a run's log — the failure context handed to the next attempt."""
    path = settings.log_dir / f"{run_id}.log"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(previous log unavailable)"
    return text[-max_chars:]


# The script the VM runs. Values arrive as environment variables so nothing needs shell
# quoting here — the prompt in particular can contain anything at all.
# Runs under /bin/sh (dash) via boxd exec, NOT bash — keep it POSIX. No `set -o pipefail`
# (a bash-ism dash rejects, which kills the script on line 1); errors are caught per-command.
REVIEW_PROMPT_TEMPLATE = """You are reviewing a pull request that another agent wrote, alone \
in a VM, with nobody else looking at it. Whether it merges depends on what you report.

Repository: {repo}
Issue #{number}: {title}
Pull request: {pr_url}
You are checked out on the branch `{branch}`. The base branch is `{base}`.

--- the issue's acceptance criteria ---
{criteria}
--- end criteria ---

Your job is to find out whether each criterion is true. Not whether the code looks correct,
not whether you would have written it that way — whether the criterion holds.

The rule that matters: **every verdict needs evidence you produced by running something.**
Evidence is a command and its output, a test name that passed, or a `file:line` you read. A
criterion you did not verify is `cannot_verify`, and `cannot_verify` counts the same as
`not_met`. Never mark something `met` because the code appears to do it — you have a whole VM
here, so run it instead.

How to check each criterion, by its `mode`:

- `test` — run the test named in `verify` on this branch. It must pass. Then confirm it would
  have caught the problem, by running it against the code as it was before this change:

      git rev-parse HEAD > /tmp/head.txt
      git checkout {base_sha} 2>/dev/null
      git checkout $(cat /tmp/head.txt) -- <the test file(s) from verify>
      <run the same test command>          # this MUST fail
      git checkout $(cat /tmp/head.txt)
      git checkout -B {branch} $(cat /tmp/head.txt)

  A new test that passes against the old code proves nothing: either the criterion was already
  satisfied and this change did not do it, or the test asserts nothing. Report the criterion
  `not_met` if that happens, and say which of the two it looked like. Skip this step only for
  criteria marked `regression: true`, which exist to prove old behaviour still works.
- `probe` and `structure` — run the command in `verify`. Exit status 0 is `met`, anything else
  is `not_met`. Quote the command and its output as evidence.
- `inspect` — read what `verify` points at and report what you found. This is the one mode that
  cannot block a merge, so be useful rather than cautious: say what is there and what is missing.

Also run the repo's fast checks once — `npm run lint`, `npm run typecheck`, `npm run test`,
`npm run test:integration`, `npm run build`. Do not run the end-to-end suite; CI covers it.
If any of them fail, that is a finding regardless of the criteria.

Then look for two specific things and report them as findings if present:

1. **Scope creep.** Map every changed file (`git diff --name-only {base_sha}...HEAD`) to a
   criterion or to the issue's stated task. Files that map to neither are findings.
2. **Rules broken.** Check the change against the repo's own written rules — `CLAUDE.md`,
   `docs/adr/*`, `.mem/`. Only rules that are actually written down. Do not invent a
   convention and then report the code for violating it; if it is not written anywhere, it is
   not a finding.

When you are done, write your verdict to **/tmp/factory-verdict.json** and nothing else:

```json
{{
  "verdict": "approve" | "request_changes",
  "criteria": [
    {{"id": "AC1", "status": "met" | "not_met" | "cannot_verify",
      "evidence": "the command you ran and what it printed, or file:line"}}
  ],
  "findings": ["one line each — what is wrong and where"],
  "notes": ["anything advisory that should not block"]
}}
```

Include every criterion by its `id`, including `inspect` ones. Say `request_changes` if any
criterion is not met or you found something that should block; `approve` only if you actually
verified everything. Do not modify the branch, do not commit, and do not open or close
anything on GitHub — writing that file is the entirety of your output.
"""


VM_SCRIPT = r"""
cd "$FACTORY_REPO_DIR" || { echo "FACTORY: repo dir $FACTORY_REPO_DIR not found" >&2; exit 90; }
git config --global --add safe.directory "$FACTORY_REPO_DIR" 2>/dev/null || true
git config user.name  "software-factory" 2>/dev/null || true
git config user.email "factory@users.noreply.github.com" 2>/dev/null || true
echo "FACTORY: fetching origin"
git fetch --prune origin || { echo "FACTORY: git fetch failed" >&2; exit 91; }
# On a retry, resume the branch a previous attempt pushed to rather than resetting to the base
# and throwing that work away. The VM is always fresh; the branch is what carries work forward.
# Only on a retry: a first attempt always starts from the base, so re-queueing an issue whose
# branch is still lying around from an earlier merged PR gets a clean start, not a resurrection.
if [ "$FACTORY_ATTEMPT" -gt 1 ] && git rev-parse --verify --quiet "origin/$FACTORY_BRANCH" > /dev/null; then
  echo "FACTORY: resuming $FACTORY_BRANCH from origin/$FACTORY_BRANCH"
  git checkout -B "$FACTORY_BRANCH" "origin/$FACTORY_BRANCH" || { echo "FACTORY: checkout failed" >&2; exit 92; }
else
  echo "FACTORY: checking out $FACTORY_BRANCH from origin/$FACTORY_BASE"
  git checkout -B "$FACTORY_BRANCH" "origin/$FACTORY_BASE" || { echo "FACTORY: checkout failed" >&2; exit 92; }
fi
# Fail fast when the machine's toolchain does not match what the repo pins. This is not
# cosmetic: two npm majors disagree about what belongs in a lock file, so a mismatched golden
# has its agents silently write lock files CI cannot install — which surfaces two steps later
# as an unrelated-looking CI failure rather than as the version problem it is. That exact
# mismatch killed CI on fourteen consecutive commits before anyone noticed. Skipped when the
# repo pins nothing or the runtime is absent, so this stays harmless for non-Node projects.
if [ -f .nvmrc ] && command -v node > /dev/null 2>&1; then
  want=$(tr -dc '0-9.' < .nvmrc | cut -d. -f1)
  have=$(node -v | tr -d 'v' | cut -d. -f1)
  if [ -n "$want" ] && [ "$want" != "$have" ]; then
    echo "FACTORY: this machine runs node $have but .nvmrc pins $want — the golden needs rebuilding" >&2
    exit 93
  fi
fi
echo "FACTORY: starting agent"
claude -p "$FACTORY_PROMPT" \
  --effort "$FACTORY_AGENT_EFFORT" \
  --disallowed-tools Agent Task ScheduleWakeup \
  --dangerously-skip-permissions \
  --output-format stream-json --verbose < /dev/null
"""


# --------------------------------------------------------------------------- log


VERDICT_PATH = "/tmp/factory-verdict.json"

# Review runs check out the PR branch and change nothing. No `-B` from the base, no push: a
# reviewer that can write to the branch is a reviewer that can make its own findings go away.
REVIEW_SCRIPT = r"""
cd "$FACTORY_REPO_DIR" || { echo "FACTORY: repo dir $FACTORY_REPO_DIR not found" >&2; exit 90; }
git config --global --add safe.directory "$FACTORY_REPO_DIR" 2>/dev/null || true
rm -f /tmp/factory-verdict.json
echo "FACTORY: fetching origin"
git fetch --prune origin || { echo "FACTORY: git fetch failed" >&2; exit 91; }
echo "FACTORY: checking out $FACTORY_BRANCH for review"
git checkout -B "$FACTORY_BRANCH" "origin/$FACTORY_BRANCH" || { echo "FACTORY: checkout failed" >&2; exit 92; }
if [ -f .nvmrc ] && command -v node > /dev/null 2>&1; then
  want=$(tr -dc '0-9.' < .nvmrc | cut -d. -f1)
  have=$(node -v | tr -d 'v' | cut -d. -f1)
  if [ -n "$want" ] && [ "$want" != "$have" ]; then
    echo "FACTORY: this machine runs node $have but .nvmrc pins $want — the golden needs rebuilding" >&2
    exit 93
  fi
fi
echo "FACTORY: starting reviewer"
claude -p "$FACTORY_PROMPT" \
  --effort "$FACTORY_AGENT_EFFORT" \
  --disallowed-tools Agent Task ScheduleWakeup \
  --dangerously-skip-permissions \
  --output-format stream-json --verbose < /dev/null
"""


def decide(verdict: dict | None, criteria: list[dict]) -> tuple[bool, str, list[str]]:
    """Turn a reviewer's verdict into a merge decision. Returns (approved, why, findings).

    The control plane decides, not the agent. The agent reports per-criterion status with
    evidence; this recomputes the outcome from those statuses, so an "approve" cannot survive a
    criterion the agent itself marked `not_met`. It can still request changes for something
    outside the criteria — a reviewer may block for a reason nobody thought to write down, but
    it may not wave one through.

    Fails closed everywhere: no verdict file, unparseable JSON, or a criterion the reviewer
    simply didn't mention all mean "not approved".
    """
    if not isinstance(verdict, dict):
        return False, "no usable verdict from the reviewer", []

    findings = [str(f) for f in (verdict.get("findings") or [])][:20]
    reported = {
        str(c.get("id")): c
        for c in (verdict.get("criteria") or [])
        if isinstance(c, dict) and c.get("id")
    }

    blocking = [c for c in criteria if c.get("mode") in BLOCKING_MODES]
    unmet, missing = [], []
    for criterion in blocking:
        cid = str(criterion["id"])
        got = reported.get(cid)
        if got is None:
            missing.append(cid)
        elif got.get("status") != "met":
            unmet.append(f"{cid} {got.get('status', 'unknown')}")

    if missing:
        return False, f"reviewer did not report on {', '.join(missing)}", findings
    if unmet:
        return False, f"criteria not met: {', '.join(unmet)}", findings
    if verdict.get("verdict") != "approve":
        return False, "reviewer requested changes", findings
    return True, f"{len(blocking)} criteria met", findings


def merge_outcome(auto_merge: bool, merged: bool, ci_failure: str | None) -> str:
    """What an approved review should do with the pull request: "done", "fix" or "human".

    Kept separate from the code that acts on it because the interesting part is the routing,
    not the doing, and the routing is where this went wrong: every ending that was not a
    merge used to be labelled `agent:done`, which left broken pull requests sitting open
    under issues that claimed to be finished.

    - "done"  — merged, or auto-merge is off and a human was always going to take it from here
    - "fix"   — CI ran and came back red, which is a defect a fix run can be sent back for
    - "human" — not merged and no failure to work from: pending, unreachable, or refused
    """
    if merged or not auto_merge:
        return "done"
    return "fix" if ci_failure else "human"


class RunLog:
    """Append-only, human-readable log for one run. The UI tails this file over SSE."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = path.open("a", encoding="utf-8", buffering=1)

    def write(self, line: str) -> None:
        self._fh.write(line.rstrip("\n") + "\n")

    def close(self) -> None:
        try:
            self._fh.close()
        except OSError:
            pass


def format_event(event: dict) -> list[str]:
    """Turn one Claude Code stream-json event into readable log lines.

    Written defensively: the event schema is not a stable contract, so anything
    unrecognised degrades to a compact fallback rather than raising.
    """
    lines: list[str] = []
    kind = event.get("type")

    if kind == "system":
        if event.get("subtype") == "init":
            lines.append(f"[agent] session {event.get('session_id', '?')} started")
        return lines

    if kind == "assistant":
        for block in event.get("message", {}).get("content", []) or []:
            btype = block.get("type")
            if btype == "text":
                text = (block.get("text") or "").strip()
                if text:
                    lines.extend(text.splitlines())
            elif btype == "tool_use":
                name = block.get("name", "tool")
                supplied = block.get("input", {}) or {}
                hint = (
                    supplied.get("command")
                    or supplied.get("file_path")
                    or supplied.get("pattern")
                    or supplied.get("skill")
                    or ""
                )
                hint = str(hint).replace("\n", " ")
                lines.append(f"[tool] {name}{': ' + hint[:160] if hint else ''}")
        return lines

    if kind == "user":
        for block in event.get("message", {}).get("content", []) or []:
            if block.get("type") == "tool_result" and block.get("is_error"):
                lines.append("[tool] -> error")
        return lines

    if kind == "result":
        usage = event.get("usage", {}) or {}
        tokens = ""
        if usage:
            tokens = (
                f" | tokens in={usage.get('input_tokens', '?')} "
                f"out={usage.get('output_tokens', '?')}"
            )
        lines.append(
            f"[agent] finished: {event.get('subtype', 'done')}"
            f" | {event.get('num_turns', '?')} turns{tokens}"
        )
        return lines

    return lines


# --------------------------------------------------------------------------- run


async def _merge(repo: str, pr_url: str, base: str, log: RunLog) -> tuple[bool, str | None]:
    """Merge a PR once its checks are green. Never raises — a merge we skip is always safer
    than one we force, and the PR simply stays open for a human.

    Returns (merged, ci_failure). `ci_failure` is set only when CI ran and came back red,
    which is a thing an agent can be sent back to fix; every other reason for not merging
    (checks still pending, GitHub unreachable, the merge itself refused) leaves it None,
    because those need a human to look rather than another run to guess.
    """
    try:
        pr_number = int(pr_url.rstrip("/").split("/")[-1])
        green, why, failed = True, "checks not required", []
        if settings.merge_require_checks:
            # The merge API waits for nothing, so without this the PR is merged seconds after
            # `gh pr create` — before CI has even started. Every run forks from main, so a red
            # main propagates into all of them.
            log.write(f"[factory] waiting for checks on PR #{pr_number}")
            sha = await github.pr_head_sha(repo, pr_number)
            green, why, failed = await github.checks_green(
                repo, sha, timeout=settings.merge_check_timeout
            )
        if not green:
            log.write(f"[factory] not merging PR #{pr_number}, left open: {why}")
            return False, why if failed else None
        await github.merge_pr(repo, pr_number)
        log.write(f"[factory] merged PR #{pr_number} into {base} ({why})")
        return True, None
    except Exception as exc:  # noqa: BLE001
        log.write(f"[factory] merge failed (PR left open): {exc!r}")
        return False, None


async def _fix_cycle(
    repo: str,
    number: int,
    golden: str,
    cycle: int,
    log: RunLog,
    *,
    reason: str,
    detail: str,
    going_back: str,
    giving_up: str,
) -> None:
    """Send a pull request back for another pass, or stop if the cycle budget is spent.

    Both things that can send one back — a reviewer that requested changes, and a CI run that
    came back red — want identical mechanics: reuse the branch so the fix builds on commits
    already pushed rather than starting over, carry the reason into the next prompt, and cap
    how many times the factory may try again unsupervised. Only the wording differs, so only
    the wording is a parameter.
    """
    if cycle < settings.max_review_cycles:
        log.write(f"[factory] {reason}; fix run {cycle + 1} scheduled")
        await _mirror_issue(repo, number, None, [], log, comment=going_back)
        await create(
            repo,
            number,
            golden=golden,
            attempt=cycle + 1,
            prior_error=reason,
            prior_log=detail,
            review_cycle=cycle + 1,
        )
    else:
        log.write(f"[factory] {reason}; cycle budget spent, halting")
        await _mirror_issue(
            repo,
            number,
            github.LABEL_BLOCKED,
            [github.LABEL_RUNNING, github.LABEL_QUEUED],
            log,
            comment=giving_up,
        )


async def _fail_run(
    run_id: str,
    repo: str,
    issue: dict,
    golden: str,
    attempt: int,
    reason: str,
    log: RunLog,
    pr_url: str | None = None,
) -> None:
    """Mark a run failed, then either schedule a retry or halt the issue.

    Retry ceiling is `settings.max_attempts`. The retry is created *before* this run is
    marked terminal, so the repo never looks idle to the poller in the gap — which would let
    the next issue in the sequence jump the queue.
    """
    number = issue["number"]
    scheduled = False
    if attempt < settings.max_attempts:
        try:
            await create(
                repo,
                number,
                golden=golden,
                attempt=attempt + 1,
                prior_error=reason,
                prior_log=_log_tail(run_id),
            )
            scheduled = True
        except Exception as exc:  # noqa: BLE001 - can't retry -> fall through and halt
            log.write(f"[factory] could not schedule retry: {exc!r}")

    await db.update_run(
        run_id, status="failed", pr_url=pr_url, error=reason, finished_at=db.utcnow()
    )

    if scheduled:
        nxt = attempt + 1
        log.write(f"[factory] attempt {attempt} failed ({reason}); retry {nxt}/{settings.max_attempts} scheduled")
        await _mirror_issue(
            repo,
            number,
            None,
            [],
            log,
            comment=f"Attempt {attempt} failed ({reason}). Retrying — attempt {nxt} of {settings.max_attempts}.",
        )
    else:
        log.write(f"[factory] attempt {attempt} failed ({reason}); no retries left, halting")
        await _mirror_issue(
            repo,
            number,
            github.LABEL_FAILED,
            [github.LABEL_RUNNING, github.LABEL_QUEUED],
            log,
            comment=f"Failed after {attempt} attempt(s) ({reason}). Halting — needs a human.",
        )


async def create(
    repo: str,
    issue_number: int,
    golden: str | None = None,
    attempt: int = 1,
    prior_error: str | None = None,
    prior_log: str | None = None,
    review_cycle: int = 1,
) -> str:
    """Register a run and schedule it. Returns the run id immediately.

    `attempt` > 1 marks a retry: the previous attempt's error and log tail are woven into
    the prompt so the agent diagnoses the failure instead of repeating it. Retries reuse the
    same branch, so the whole chain resolves into one pull request.
    """
    issue = await github.get_issue(repo, issue_number)
    run_id = uuid.uuid4().hex
    branch = f"factory/issue-{issue_number}"
    log_path = settings.log_dir / f"{run_id}.log"
    log_path.touch()

    await db.create_run(
        id=run_id,
        repo=repo,
        issue_number=issue_number,
        issue_title=issue["title"],
        branch=branch,
        golden=golden or settings.golden,
        status="queued",
        attempt=attempt,
        agent=AGENT,
        log_path=str(log_path),
        created_at=db.utcnow(),
    )

    task = asyncio.create_task(
        _guarded(
            run_id, repo, issue, branch, golden or settings.golden,
            attempt, prior_error, prior_log, review_cycle,
        )
    )
    _tasks[run_id] = task
    task.add_done_callback(lambda _t: _tasks.pop(run_id, None))
    return run_id


async def create_review(
    repo: str,
    issue_number: int,
    pr_url: str,
    branch: str,
    golden: str | None = None,
    cycle: int = 1,
) -> str:
    """Register and schedule a review of the pull request a build run opened."""
    issue = await github.get_issue(repo, issue_number)
    run_id = uuid.uuid4().hex
    log_path = settings.log_dir / f"{run_id}.log"
    log_path.touch()

    await db.create_run(
        id=run_id,
        repo=repo,
        issue_number=issue_number,
        issue_title=issue["title"],
        branch=branch,
        golden=golden or settings.golden,
        status="queued",
        kind="review",
        attempt=cycle,
        agent=AGENT,
        pr_url=pr_url,
        log_path=str(log_path),
        created_at=db.utcnow(),
    )

    task = asyncio.create_task(
        _guarded_review(run_id, repo, issue, branch, pr_url, golden or settings.golden, cycle)
    )
    _tasks[run_id] = task
    task.add_done_callback(lambda _t: _tasks.pop(run_id, None))
    return run_id


async def _salvage_usage(run_id: str, log: RunLog) -> None:
    """Backstop the run's ledger entry from the telemetry rows.

    A run only reports its own usage in the final `result` event, so a timeout, a crash
    or a cancelled task used to leave `cost_usd = NULL` — real money spent, recorded as
    nothing, on exactly the runs worth understanding. The per-call rows were written as
    the run went, so the spend is still there; this reads it back and fills the gap.

    Only ever fills a gap: a run that reported its own numbers keeps them, so the
    runtime's figure stays authoritative wherever it exists and the derived one is
    strictly a fallback. Best effort by design — never raises.
    """
    try:
        run = await db.get_run(run_id)
        if not run or run.get("cost_usd") is not None:
            return
        derived = await Recorder(run_id).totals()
        if not derived:
            return
        await db.update_run(run_id, **derived)
        log.write(
            f"[factory] usage recovered from telemetry: "
            f"${derived.get('cost_usd') or 0:.2f}, "
            f"{derived.get('tokens_out') or 0} output tokens"
        )
    except asyncio.CancelledError:
        # Called from a `finally` that may already be unwinding a cancelled run. The
        # caller has re-raised, so cancellation still propagates; give up on the number
        # rather than block the teardown.
        return
    except Exception as exc:  # noqa: BLE001 - a missing number must not mask the failure
        log.write(f"[factory] usage salvage skipped: {exc!r}")


async def _guarded_review(
    run_id: str,
    repo: str,
    issue: dict,
    branch: str,
    pr_url: str,
    golden: str,
    cycle: int,
) -> None:
    log = RunLog(Path(settings.log_dir / f"{run_id}.log"))
    try:
        async with semaphore():
            await _execute_review(run_id, repo, issue, branch, pr_url, golden, log, cycle)
    except asyncio.CancelledError:
        log.write("[factory] review cancelled")
        await db.update_run(run_id, status="cancelled", finished_at=db.utcnow())
        raise
    except Exception as exc:  # noqa: BLE001
        # A review that crashes must not merge anything, and must not silently strand the PR
        # either. Leave it open, labelled, for a human.
        log.write(f"[factory] review failed: {exc!r}")
        reason = (
            f"timed out after {settings.run_timeout}s"
            if isinstance(exc, asyncio.TimeoutError)
            else f"crashed: {str(exc)[:200] or type(exc).__name__}"
        )
        await db.update_run(
            run_id, status="failed", error=reason, finished_at=db.utcnow()
        )
        await _mirror_issue(
            repo,
            issue["number"],
            github.LABEL_BLOCKED,
            [github.LABEL_RUNNING],
            log,
            comment=f"Review run {reason}. {pr_url} is open and unreviewed — needs a human.",
        )
    finally:
        await _salvage_usage(run_id, log)
        log.close()


async def _guarded(
    run_id: str,
    repo: str,
    issue: dict,
    branch: str,
    golden: str,
    attempt: int,
    prior_error: str | None,
    prior_log: str | None,
    review_cycle: int = 1,
) -> None:
    log = RunLog(Path(settings.log_dir / f"{run_id}.log"))
    try:
        async with semaphore():
            await _execute(
                run_id, repo, issue, branch, golden, log,
                attempt, prior_error, prior_log, review_cycle,
            )
    except asyncio.CancelledError:
        # A human stopped this run. Do not retry — cancellation is a decision, not a failure.
        log.write("[factory] run cancelled")
        await db.update_run(run_id, status="cancelled", finished_at=db.utcnow())
        raise
    except Exception as exc:  # noqa: BLE001 - the UI is where failures get reported
        log.write(f"[factory] run failed: {exc!r}")
        # str() on an asyncio.TimeoutError is empty, which used to record the least useful
        # error in the table ("crashed: ") for the one failure mode that says exactly what
        # happened. Name it, and fall back to the exception class for anything else silent.
        if isinstance(exc, asyncio.TimeoutError):
            reason = f"timed out after {settings.run_timeout}s"
        else:
            reason = f"crashed: {str(exc)[:200] or type(exc).__name__}"
        await _fail_run(run_id, repo, issue, golden, attempt, reason, log)
    finally:
        await _salvage_usage(run_id, log)
        log.close()


async def _execute(
    run_id: str,
    repo: str,
    issue: dict,
    branch: str,
    golden: str,
    log: RunLog,
    attempt: int = 1,
    prior_error: str | None = None,
    prior_log: str | None = None,
    review_cycle: int = 1,
) -> None:
    boxd = client()
    machine = None
    number = issue["number"]
    try:
        base = await github.default_branch(repo)
        prompt = build_prompt(repo, issue, branch, base, attempt, prior_error, prior_log)

        # ---- claim: mirror the pickup onto the issue for anyone watching on GitHub
        which = f" (attempt {attempt} of {settings.max_attempts})" if attempt > 1 else ""
        started = f"Factory run started on branch `{branch}`{which}."
        link = _run_link(run_id)
        if link:
            started += f"\n\nLive log: {link}"
        await _mirror_issue(
            repo, number, github.LABEL_RUNNING, [github.LABEL_QUEUED], log, comment=started
        )

        # ---- fork
        await db.update_run(run_id, status="forking", started_at=db.utcnow())
        vm_name = f"run-{run_id[:8]}"
        log.write(f"[factory] forking {golden} -> {vm_name}")
        source_id = await golden_id(boxd, golden)
        machine = await boxd.machines.fork(
            source_id,
            vm_name,
            # Agent work is CPU-bound and often silent; the default idle suspend would
            # freeze the VM mid-build. Zero disables it.
            auto_suspend_timeout=0,
            # Safety net: if this process dies, the VM still reaps itself.
            auto_destroy_timeout=settings.auto_destroy,
        )
        await db.update_run(run_id, vm_name=machine.name, vm_id=machine.id)
        await boxd.machines.wait_until_ready(machine.id, timeout=180)
        log.write(f"[factory] {machine.name} ready ({machine.id})")

        # ---- run the agent
        await db.update_run(run_id, status="running")
        env = {
            "FACTORY_REPO_DIR": settings.repo_dir,
            "FACTORY_BRANCH": branch,
            "FACTORY_BASE": base,
            "FACTORY_ATTEMPT": str(attempt),
            "FACTORY_PROMPT": prompt,
            "FACTORY_AGENT_EFFORT": settings.agent_effort,
            # How long a single shell command may run before the agent's own tooling moves
            # it to the background. The default (120s) is shorter than an ordinary build or
            # test run here, and a backgrounded command is what the agent then waits on
            # forever — under `claude -p` there is no loop to deliver the notification it
            # expects. Generous values keep everything legitimate in the foreground; a
            # genuinely stuck command is bounded by FACTORY_RUN_TIMEOUT, not by this.
            "BASH_DEFAULT_TIMEOUT_MS": str(settings.bash_default_timeout * 1000),
            "BASH_MAX_TIMEOUT_MS": str(settings.bash_max_timeout * 1000),
            # Correlation key. Telemetry is not wired yet, but every run carries its id
            # so traces can attach without changing the dispatch contract later.
            "OTEL_RESOURCE_ATTRIBUTES": (
                f"run.id={run_id},issue={repo}#{issue['number']},repo={repo},vm={machine.name}"
            ),
        }
        # Durable auth for the agent's `claude`, overriding the golden's expiring OAuth.
        if settings.anthropic_api_key:
            env["ANTHROPIC_API_KEY"] = settings.anthropic_api_key
        if settings.claude_code_oauth_token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = settings.claude_code_oauth_token
        exit_code, usage = await asyncio.wait_for(
            _stream(boxd, machine.id, env, log, run_id), timeout=settings.run_timeout
        )
        log.write(f"[factory] agent exited {exit_code}")
        await db.update_run(
            run_id,
            exit_code=exit_code,
            tokens_in=usage.get("tokens_in"),
            tokens_out=usage.get("tokens_out"),
            cost_usd=usage.get("cost_usd"),
        )

        # ---- collect
        pr_url = await github.find_pr(repo, branch)
        if pr_url:
            log.write(f"[factory] pull request: {pr_url}")
        else:
            log.write("[factory] no pull request found for this branch")
        await _salvage_transcript(boxd, machine.id, run_id, log)

        ok = exit_code == 0 and pr_url is not None
        review_next = False
        if ok:
            criteria = parse_criteria(issue.get("body") or "")
            review_next = settings.review_enabled and bool(criteria)
            merged = False
            if review_next:
                # Hand the PR to a reviewing agent instead of merging on the builder's word.
                # An issue with no machine-readable criteria has nothing to review against, so
                # it falls through to the old path rather than being reviewed against a guess.
                log.write(f"[factory] {len(criteria)} acceptance criteria; queueing review")
            elif settings.auto_merge:
                # Merge now so the next issue in the sequence branches from a main that
                # already contains this issue's work.
                if not criteria:
                    log.write("[factory] issue carries no acceptance criteria; skipping review")
                merged, _ = await _merge(repo, pr_url, base, log)
            await db.update_run(
                run_id, status="succeeded", pr_url=pr_url, error=None, finished_at=db.utcnow()
            )
            outcome = f"Factory run finished. Pull request: {pr_url}"
            if merged:
                outcome += " (auto-merged)"
            elif review_next:
                outcome += " — under review"
            await _mirror_issue(
                repo,
                number,
                None if review_next else github.LABEL_DONE,
                [] if review_next else [github.LABEL_RUNNING],
                log,
                comment=outcome,
            )
        else:
            reason = f"exit {exit_code}, {'no ' if not pr_url else ''}pull request"
            await _fail_run(run_id, repo, issue, golden, attempt, reason, log, pr_url=pr_url)

        # ---- reap
        if ok or not settings.keep_failed:
            # Brief drain so any buffered telemetry flushes before the VM disappears.
            await asyncio.sleep(3)
            await boxd.machines.delete(machine.id)
            log.write(f"[factory] destroyed {machine.name}")
        else:
            log.write(f"[factory] keeping {machine.name} for inspection (FACTORY_KEEP_FAILED=1)")

        # ---- hand off to review, once this run's VM is gone and its slot is free
        if ok and review_next:
            try:
                await create_review(repo, number, pr_url, branch, golden, cycle=review_cycle)
            except Exception as exc:  # noqa: BLE001 - an unreviewed PR beats a lost one
                log.write(f"[factory] could not queue review: {exc!r}")
    finally:
        await boxd.close()


async def _execute_review(
    run_id: str,
    repo: str,
    issue: dict,
    branch: str,
    pr_url: str,
    golden: str,
    log: RunLog,
    cycle: int,
) -> None:
    """Fork a VM, review the PR against the issue's criteria, then merge it or send it back."""
    number = issue["number"]
    base = await github.default_branch(repo)
    criteria = parse_criteria(issue.get("body") or "")
    boxd = client()
    try:
        await db.update_run(run_id, status="forking", started_at=db.utcnow())
        vm_name = f"rev-{run_id[:8]}"
        log.write(f"[factory] review {cycle}/{settings.max_review_cycles} of {pr_url}")
        log.write(f"[factory] forking {golden} -> {vm_name}")
        machine = await boxd.machines.fork(
            await golden_id(boxd, golden),
            vm_name,
            auto_suspend_timeout=0,
            auto_destroy_timeout=settings.auto_destroy,
        )
        await db.update_run(run_id, vm_name=machine.name, vm_id=machine.id)
        await boxd.machines.wait_until_ready(machine.id, timeout=180)
        log.write(f"[factory] {machine.name} ready ({machine.id})")

        pr_number = int(pr_url.rstrip("/").split("/")[-1])
        head_sha = await github.pr_head_sha(repo, pr_number)
        base_sha = await github.merge_base_sha(repo, base, head_sha)

        await db.update_run(run_id, status="running")
        prompt = REVIEW_PROMPT_TEMPLATE.format(
            repo=repo,
            number=number,
            title=issue["title"],
            pr_url=pr_url,
            branch=branch,
            base=base,
            base_sha=base_sha,
            criteria=yaml.safe_dump(criteria, sort_keys=False, allow_unicode=True),
        )
        env = {
            "FACTORY_REPO_DIR": settings.repo_dir,
            "FACTORY_BRANCH": branch,
            "FACTORY_BASE": base,
            "FACTORY_PROMPT": prompt,
            "FACTORY_AGENT_EFFORT": settings.agent_effort,
            "BASH_DEFAULT_TIMEOUT_MS": str(settings.bash_default_timeout * 1000),
            "BASH_MAX_TIMEOUT_MS": str(settings.bash_max_timeout * 1000),
            "OTEL_RESOURCE_ATTRIBUTES": (
                f"run.id={run_id},issue={repo}#{number},repo={repo},vm={machine.name},kind=review"
            ),
        }
        if settings.anthropic_api_key:
            env["ANTHROPIC_API_KEY"] = settings.anthropic_api_key
        if settings.claude_code_oauth_token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = settings.claude_code_oauth_token

        exit_code, usage = await asyncio.wait_for(
            _stream(boxd, machine.id, env, log, run_id, script=REVIEW_SCRIPT),
            timeout=settings.run_timeout,
        )
        log.write(f"[factory] reviewer exited {exit_code}")
        verdict = await _read_verdict(boxd, machine.id, log)
        await _salvage_transcript(boxd, machine.id, run_id, log)

        approved, why, findings = decide(verdict, criteria)
        await db.update_run(
            run_id,
            exit_code=exit_code,
            tokens_in=usage.get("tokens_in"),
            tokens_out=usage.get("tokens_out"),
            cost_usd=usage.get("cost_usd"),
            verdict=json.dumps(verdict) if verdict else None,
            status="succeeded",
            error=None if approved else why,
            finished_at=db.utcnow(),
        )
        log.write(f"[factory] verdict: {'approve' if approved else 'request changes'} — {why}")
        for finding in findings:
            log.write(f"[factory]   finding: {finding}")

        await asyncio.sleep(3)
        await boxd.machines.delete(machine.id)
        log.write(f"[factory] destroyed {machine.name}")

        # ---- act on the verdict
        #
        # An approved pull request has three possible endings, and they are not the same
        # thing. Merged is finished. Red CI is a real defect in code a reviewer signed off,
        # which is exactly what a fix run is for. Anything else — checks still pending,
        # GitHub unreachable, the merge itself refused — means we do not know what is wrong,
        # so it stops for a human. Collapsing all three into `agent:done` is how a broken
        # pull request ends up sitting open under an issue that claims to be finished.
        if approved:
            merged, ci_failure = False, None
            if settings.auto_merge:
                merged, ci_failure = await _merge(repo, pr_url, base, log)
            outcome = merge_outcome(settings.auto_merge, merged, ci_failure)

            if outcome == "done":
                comment = f"Review passed — {why}. {pr_url}"
                if merged:
                    comment += " (merged)"
                await _mirror_issue(
                    repo, number, github.LABEL_DONE, [github.LABEL_RUNNING], log, comment=comment
                )
                return

            if outcome == "human":
                await db.update_run(run_id, error="merge blocked, cause unknown")
                await _mirror_issue(
                    repo,
                    number,
                    github.LABEL_BLOCKED,
                    [github.LABEL_RUNNING, github.LABEL_QUEUED],
                    log,
                    comment=(
                        f"Review passed — {why} — but the pull request could not be merged and "
                        f"CI never reported a failure to work from. Needs a human. {pr_url}"
                    ),
                )
                return

            await db.update_run(run_id, error=ci_failure)
            await _fix_cycle(
                repo,
                number,
                golden,
                cycle,
                log,
                reason=f"CI failed after an approved review: {ci_failure}",
                detail=(
                    f"CI failed on the pull request after the review approved it.\n"
                    f"{ci_failure}\n\n"
                    "The reviewer confirmed every acceptance criterion against real command "
                    "output, so the change itself is sound. What failed is something CI runs "
                    "that this VM does not — the end-to-end browser suite in particular.\n\n"
                    "Read the actual failure before changing anything:\n"
                    f"  gh run list --branch {branch} --limit 1\n"
                    "  gh run view <run-id> --log-failed\n\n"
                    "Then fix its cause. Do not delete, skip or weaken a test to make it pass, "
                    "and do not edit CI configuration — a gate that fails is doing its job."
                ),
                going_back=(
                    f"Review passed, but CI is red ({ci_failure}). Fixing. {pr_url}"
                ),
                giving_up=(
                    f"Review passed, but CI is still red after {cycle} cycles ({ci_failure}).\n\n"
                    f"Stopping — needs a human. {pr_url}"
                ),
            )
            return

        detail = "\n".join(f"- {f}" for f in findings) or "- (no specific findings recorded)"
        await _fix_cycle(
            repo,
            number,
            golden,
            cycle,
            log,
            reason=f"review requested changes: {why}",
            detail=f"Reviewer findings:\n{detail}",
            going_back=f"Review {cycle} requested changes ({why}):\n{detail}\n\nFixing.",
            giving_up=(
                f"Review still requesting changes after {cycle} cycles ({why}):\n{detail}"
                f"\n\nStopping — the issue may be wrong rather than the code. {pr_url}"
            ),
        )
    finally:
        await boxd.close()


async def _read_verdict(boxd: AsyncBoxd, machine_id: str, log: RunLog) -> dict | None:
    """Read the verdict file out of the VM. Any failure returns None, which `decide` treats
    as "not approved" — a reviewer that produced nothing readable has approved nothing."""
    try:
        result = await boxd.machines.exec(machine_id, f"cat {VERDICT_PATH}", timeout=30)
        raw = getattr(result, "stdout", None) or ""
        if inspect.isawaitable(raw):  # pragma: no cover - SDK shape drift
            raw = await raw
        text = raw.strip()
        if not text:
            log.write("[factory] reviewer wrote no verdict file")
            return None
        # The agent sometimes wraps JSON in a ``` fence despite being asked not to.
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\n|\n```$", "", text.strip())
        return json.loads(text)
    except json.JSONDecodeError as exc:
        log.write(f"[factory] verdict file is not valid JSON: {exc}")
        return None
    except Exception as exc:  # noqa: BLE001
        log.write(f"[factory] could not read verdict: {exc!r}")
        return None


async def _stream(
    boxd: AsyncBoxd,
    machine_id: str,
    env: dict,
    log: RunLog,
    run_id: str,
    script: str = VM_SCRIPT,
) -> tuple[int, dict]:
    """Run the agent, formatting its event stream into the log as it arrives.

    Returns the exit code and the usage captured from the final `result` event
    (input/output tokens and cost), which is what the Runs UI shows as the run's cost.

    The same events also go to the telemetry recorder, which normalizes them into
    per-call rows and writes them as the run proceeds. That is a second consumer of a
    stream we were already parsing, not a second stream: the facts telemetry wants are
    the ones scrolling past here, and the only thing that used to happen to them was
    being formatted into a log line and forgotten.
    """
    buffer = ""
    usage: dict = {}
    recorder = Recorder(run_id)
    async with boxd.machines.stream_exec(
        machine_id, command=script, env=env, close_stdin=True
    ) as stream:
        async for chunk in stream.iter_chunks():
            text = chunk.data.decode("utf-8", errors="replace")
            if chunk.is_stderr:
                for line in text.splitlines():
                    if line.strip():
                        log.write(f"[stderr] {line}")
                continue
            buffer += text
            while "\n" in buffer:
                line, _, buffer = buffer.partition("\n")
                line = line.strip()
                if not line:
                    continue
                if line.startswith("{"):
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        log.write(line)
                        continue
                    if event.get("type") == "result":
                        u = event.get("usage", {}) or {}
                        usage = {
                            "tokens_in": u.get("input_tokens"),
                            "tokens_out": u.get("output_tokens"),
                            "cost_usd": event.get("total_cost_usd"),
                        }
                    await recorder.feed(event)
                    for formatted in format_event(event):
                        log.write(formatted)
                    continue
                log.write(line)
        code = stream.exit_code
        if inspect.isawaitable(code):
            code = await code
    await recorder.close()
    if recorder.dropped:
        log.write(f"[factory] telemetry dropped {recorder.dropped} events")
    return int(code or 0), usage


async def _salvage_transcript(boxd: AsyncBoxd, machine_id: str, run_id: str, log: RunLog) -> None:
    """Copy the agent's session transcript out before the VM is destroyed.

    The live stream is for watching; this file is the complete, replayable record. Best
    effort by design — a missing transcript must never fail an otherwise good run.
    """
    script = (
        'f=$(ls -t "$HOME"/.claude/projects/*/*.jsonl 2>/dev/null | head -1); '
        '[ -n "$f" ] && [ "$(wc -c < "$f")" -lt 20000000 ] && cat "$f" || true'
    )
    try:
        result = await boxd.machines.exec(machine_id, script, timeout=120)
        if result.stdout.strip():
            path = settings.log_dir / f"{run_id}.transcript.jsonl"
            path.write_text(result.stdout, encoding="utf-8")
            await db.update_run(run_id, transcript_path=str(path))
            log.write(f"[factory] transcript saved ({len(result.stdout)} bytes)")
    except Exception as exc:  # noqa: BLE001
        log.write(f"[factory] transcript salvage skipped: {exc!r}")


async def cancel(run_id: str) -> bool:
    """Cancel an in-flight run and destroy its VM."""
    run = await db.get_run(run_id)
    if not run or run["status"] in db.TERMINAL:
        return False
    task = _tasks.get(run_id)
    if task:
        task.cancel()
    if run.get("vm_id"):
        boxd = client()
        try:
            await boxd.machines.delete(run["vm_id"])
        except Exception:  # noqa: BLE001 - already gone is fine
            pass
        finally:
            await boxd.close()
    await db.update_run(run_id, status="cancelled", finished_at=db.utcnow())
    return True


async def reconcile() -> dict:
    """Compare the boxd fleet against the runs table and resolve the difference.

    Fleet state belongs in the database, not in anybody's head. Without this, a crashed
    dispatch silently leaks machines against the quota.
    """
    boxd = client()
    try:
        machines = await boxd.machines.list()
        active = await db.active_runs()
        active_vms = {r["vm_name"] for r in active if r.get("vm_name")}

        orphans = [
            m for m in machines if m.name.startswith("run-") and m.name not in active_vms
        ]
        for machine in orphans:
            await boxd.machines.delete(machine.id)

        # Runs we think are live whose VM no longer exists.
        names = {m.name for m in machines}
        stranded = []
        for run in active:
            if run.get("vm_name") and run["vm_name"] not in names and run["id"] not in _tasks:
                await db.update_run(
                    run["id"],
                    status="failed",
                    error="VM disappeared while run was active",
                    finished_at=db.utcnow(),
                )
                stranded.append(run["id"])
        return {"destroyed": [m.name for m in orphans], "stranded": stranded}
    finally:
        await boxd.close()
