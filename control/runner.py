"""Fork a VM, run an agent in it, collect the result, reap the VM.

This module is the whole factory. It contains no model call: it decides nothing about the
work itself, only about machine lifecycle. All intelligence is the agent inside the VM.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import uuid
from pathlib import Path

from boxd import AsyncBoxd

from . import db, github
from .config import settings

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
3. Run whatever tests or checks the repo already has. Do not add a test framework that
   isn't already there.
4. Record anything durable you learned into `.mem/`, following the memory skill.
5. Commit on the branch you are already on ({branch}) and push it:
   git add -A && git commit -m "<message>" && git push -u origin {branch}
6. Open a pull request with `gh pr create --fill --base {base}`, and reference the issue
   in the body so it links (e.g. "Closes #{number}").

If you cannot resolve the issue, still push what you have and open a draft PR explaining
what blocked you. A run that ends with no PR gives the human nothing to look at.
"""


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

A previous attempt may already have pushed commits to {branch}. If a normal `git push` is
rejected as non-fast-forward, reconcile and force-push with `git push --force-with-lease`.

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
VM_SCRIPT = r"""
set -o pipefail
cd "$FACTORY_REPO_DIR" || { echo "FACTORY: repo dir $FACTORY_REPO_DIR not found" >&2; exit 90; }
git config --global --add safe.directory "$FACTORY_REPO_DIR" 2>/dev/null || true
git config user.name  "software-factory" 2>/dev/null || true
git config user.email "factory@users.noreply.github.com" 2>/dev/null || true
echo "FACTORY: fetching origin"
git fetch --prune origin || { echo "FACTORY: git fetch failed" >&2; exit 91; }
echo "FACTORY: checking out $FACTORY_BRANCH from origin/$FACTORY_BASE"
git checkout -B "$FACTORY_BRANCH" "origin/$FACTORY_BASE" || { echo "FACTORY: checkout failed" >&2; exit 92; }
echo "FACTORY: starting agent"
claude -p "$FACTORY_PROMPT" \
  --dangerously-skip-permissions \
  --output-format stream-json --verbose < /dev/null
"""


# --------------------------------------------------------------------------- log


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
        log_path=str(log_path),
        created_at=db.utcnow(),
    )

    task = asyncio.create_task(
        _guarded(run_id, repo, issue, branch, golden or settings.golden, attempt, prior_error, prior_log)
    )
    _tasks[run_id] = task
    task.add_done_callback(lambda _t: _tasks.pop(run_id, None))
    return run_id


async def _guarded(
    run_id: str,
    repo: str,
    issue: dict,
    branch: str,
    golden: str,
    attempt: int,
    prior_error: str | None,
    prior_log: str | None,
) -> None:
    log = RunLog(Path(settings.log_dir / f"{run_id}.log"))
    try:
        async with semaphore():
            await _execute(run_id, repo, issue, branch, golden, log, attempt, prior_error, prior_log)
    except asyncio.CancelledError:
        # A human stopped this run. Do not retry — cancellation is a decision, not a failure.
        log.write("[factory] run cancelled")
        await db.update_run(run_id, status="cancelled", finished_at=db.utcnow())
        raise
    except Exception as exc:  # noqa: BLE001 - the UI is where failures get reported
        log.write(f"[factory] run failed: {exc!r}")
        await _fail_run(run_id, repo, issue, golden, attempt, f"crashed: {str(exc)[:200]}", log)
    finally:
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
        machine = await boxd.machines.fork(
            golden,
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
            "FACTORY_PROMPT": prompt,
            # Correlation key. Telemetry is not wired yet, but every run carries its id
            # so traces can attach without changing the dispatch contract later.
            "OTEL_RESOURCE_ATTRIBUTES": (
                f"run.id={run_id},issue={repo}#{issue['number']},repo={repo},vm={machine.name}"
            ),
        }
        exit_code = await asyncio.wait_for(
            _stream(boxd, machine.id, env, log), timeout=settings.run_timeout
        )
        log.write(f"[factory] agent exited {exit_code}")
        await db.update_run(run_id, exit_code=exit_code)

        # ---- collect
        pr_url = await github.find_pr(repo, branch)
        if pr_url:
            log.write(f"[factory] pull request: {pr_url}")
        else:
            log.write("[factory] no pull request found for this branch")
        await _salvage_transcript(boxd, machine.id, run_id, log)

        ok = exit_code == 0 and pr_url is not None
        if ok:
            await db.update_run(
                run_id, status="succeeded", pr_url=pr_url, error=None, finished_at=db.utcnow()
            )
            outcome = f"Factory run finished. Pull request: {pr_url}"
            await _mirror_issue(
                repo, number, github.LABEL_DONE, [github.LABEL_RUNNING], log, comment=outcome
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
    finally:
        await boxd.close()


async def _stream(boxd: AsyncBoxd, machine_id: str, env: dict, log: RunLog) -> int:
    """Run the agent, formatting its event stream into the log as it arrives."""
    buffer = ""
    async with boxd.machines.stream_exec(
        machine_id, command=VM_SCRIPT, env=env, close_stdin=True
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
                        for formatted in format_event(json.loads(line)):
                            log.write(formatted)
                        continue
                    except json.JSONDecodeError:
                        pass
                log.write(line)
        code = stream.exit_code
        if inspect.isawaitable(code):
            code = await code
    return int(code or 0)


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
