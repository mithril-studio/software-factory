"""Warm a golden for one repo: restore the base, clone, install, capture, destroy.

A repo's golden is `golden-<repo-slug>` — the base image with that repo already cloned and its
dependencies installed. It is a **speed-up and nothing else**: a repo without one dispatches
onto `golden-copy` and installs for itself, which is what lets a repo be connected and
dispatched in the same minute. So everything here is allowed to be slow, allowed to fail, and
never allowed to block a run.

`scripts/refresh-golden.sh` does the same four steps by hand and is not what runs here.
`control/README.md` §2 forbids shelling out to the `boxd` binary from the control plane — the
SDK gives typed errors, connection reuse and streaming exec, where the binary gives stdout to
parse and auto-updates underneath you. What the two share on purpose is the *policy*: the
install command comes from the repo's own `## Setup` section and is never guessed.

## Always from the base

Even when the repo already has a golden. Restoring its own snapshot and updating it in place
would be faster, and it is what the shell script does — but it also carries forward whatever
the last capture got wrong, and a warm golden that is subtly broken costs more than a cold one
that is merely slow. Building from `golden-copy` every time means re-provisioning is the repair
for a bad snapshot rather than another way to inherit it.

Re-saving under an existing name is safe: boxd captures a new version and the previous one
stays restorable, so a run dispatched mid-provision keeps working.

## Modelled as a run

A `runs` row with `kind = 'provision'`, which is not a stretch — it is an agentless run on a
VM restored from a golden, and it wants exactly what the other two kinds already have: a
streamed log the UI can tail, a cancel button, a status in the runs table, and a VM the
reconciler recognises as ours. Its `issue_number` is 0 because there is no issue; nothing
about a provisioning run touches GitHub beyond the clone.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from pathlib import Path

from . import agents, db, github, preflight, repos, runner
from .config import settings

log = logging.getLogger("factory.provision")

# What `repos.provision_status` holds. `none` is the state a repo is registered in, and the
# state it returns to if its golden is deleted — not an error, just the cold path.
NONE, RUNNING, READY, FAILED = "none", "running", "ready", "failed"

# Clone, check out the default branch, repair the toolchain if the repo pins one, install.
#
# `eval "$FACTORY_SETUP"` rather than the command interpolated into this text: the SDK
# `shlex.quote`s every value it puts in the exec prefix, so the command crosses the wire as one
# argument no matter what it contains. `eval` then runs it as the command line the repo wrote,
# which is the intent — it is the same command `scripts/refresh-golden.sh` runs and the same
# one the agent is told to run at the top of every build.
SCRIPT = runner.PRELUDE + r"""
echo "FACTORY: checking out $FACTORY_BASE"
git checkout -B "$FACTORY_BASE" "origin/$FACTORY_BASE" \
  || { echo "FACTORY: checkout failed" >&2; exit 92; }
""" + runner.NODE_GUARD + r"""
echo "FACTORY: installing with: $FACTORY_SETUP"
eval "$FACTORY_SETUP" || { echo "FACTORY: setup command failed" >&2; exit 94; }
echo "FACTORY: install finished"
"""


async def _stream(boxd, machine_id: str, script: str, env: dict, run_log: runner.RunLog) -> int:
    """Run a shell script in the VM, copying its output into the run log line by line.

    Deliberately not `runner._stream_agent`: there is no agent here, so there are no JSON
    events to parse, no manifest to read and no telemetry to record. Feeding plain shell output
    through the agent's stream parser would file every line as an unrecognised event.

    Returns the script's exit status.
    """
    buffer = ""
    async with boxd.machines.stream_exec(
        machine_id,
        command=runner.export_prelude(env) + script,
        env=env,
        close_stdin=True,
    ) as stream:
        async for chunk in stream.iter_chunks():
            text = chunk.data.decode("utf-8", errors="replace")
            if chunk.is_stderr:
                for line in text.splitlines():
                    if line.strip():
                        run_log.write(f"[stderr] {line}")
                continue
            buffer += text
            while "\n" in buffer:
                line, _, buffer = buffer.partition("\n")
                if line.strip():
                    run_log.write(line)
        if buffer.strip():
            run_log.write(buffer)
        # Property on some SDK versions, coroutine on others — the same shape `runner`
        # already handles, and for the same reason: pinning it would pin the SDK.
        code = stream.exit_code
        if inspect.isawaitable(code):
            code = await code
    return int(code or 0)


async def setup_command(repo: str, base: str) -> str:
    """The install command `repo` names in its `.factory.md`, read from GitHub.

    Read here rather than inside the VM because the answer decides whether to start a VM at
    all: a repo that names no setup command cannot be provisioned, and finding that out after
    restoring a machine costs a slot and a minute for a refusal that was knowable up front.
    """
    return preflight.setup_command(await github.file(repo, runner.PROFILE_PATH, base))


async def create(repo: str) -> str:
    """Register a provisioning run for `repo` and schedule it. Returns the run id.

    Refuses before spending anything when the repo names no setup command — there is nothing
    to install, and guessing is the failure mode this whole path is built to avoid.
    """
    repo = repo.strip()
    base = await github.default_branch(repo)
    command = await setup_command(repo, base)
    if not command:
        raise ValueError(
            f"{repo} names no setup command in {runner.PROFILE_PATH}, so there is nothing to "
            "install into a golden. Add a `## Setup` section naming it in backticks. Until "
            f"then its runs boot {agents.BASE_SNAPSHOT} and install for themselves."
        )
    if "\n" in command:
        raise ValueError(f"{repo}'s setup command spans more than one line: {command!r}")

    run_id = uuid.uuid4().hex
    log_path = settings.log_dir / f"{run_id}.log"
    log_path.touch()
    await db.create_run(
        id=run_id,
        repo=repo,
        issue_number=0,
        issue_title=f"provision {agents.golden_name(repo)}",
        status="queued",
        kind="provision",
        agent=agents.DEFAULT_AGENT,
        log_path=str(log_path),
        created_at=db.utcnow(),
    )
    await repos.record_golden(repo, None, RUNNING)

    runner.track(run_id, asyncio.create_task(_guarded(run_id, repo, base, command)))
    return run_id


async def _guarded(run_id: str, repo: str, base: str, command: str) -> None:
    run_log = runner.RunLog(Path(settings.log_dir / f"{run_id}.log"))
    try:
        async with runner.semaphore():
            await _execute(run_id, repo, base, command, run_log)
    except asyncio.CancelledError:
        run_log.write("[factory] provisioning cancelled")
        await db.update_run(run_id, status="cancelled", finished_at=db.utcnow())
        await repos.record_golden(repo, None, NONE)
        raise
    except Exception as exc:  # noqa: BLE001 - a failed warm-up is a slower repo, not an outage
        reason = f"crashed: {str(exc)[:200] or type(exc).__name__}"
        run_log.write(f"[factory] provisioning failed: {exc!r}")
        await db.update_run(run_id, status="failed", error=reason, finished_at=db.utcnow())
        await repos.record_golden(repo, None, FAILED)
    finally:
        run_log.close()


async def _execute(run_id: str, repo: str, base: str, command: str, run_log: runner.RunLog) -> None:
    snapshot = agents.golden_name(repo)
    vm_name = f"{runner.PROVISION_PREFIX}{run_id[:8]}"
    boxd = runner.client()
    machine = None
    try:
        await db.update_run(run_id, status="forking", started_at=db.utcnow(), golden=snapshot)
        run_log.write(f"[factory] warming {snapshot} for {repo} from {agents.BASE_SNAPSHOT}")
        machine = await runner._provision(boxd, agents.BASE_SNAPSHOT, vm_name)
        await db.update_run(run_id, vm_name=machine.name, vm_id=machine.id)
        await boxd.machines.wait_until_ready(machine.id, timeout=180)
        run_log.write(f"[factory] {machine.name} ready ({machine.id})")

        await db.update_run(run_id, status="running")
        env = {
            "FACTORY_REPO": repo,
            "FACTORY_WORKDIR": settings.workdir,
            # Never reuse a pre-clone checkout when building a golden. That override exists so
            # a run can work in a machine that already holds the right repo; here the machine
            # is the base image and anything at that path belongs to some other repo.
            "FACTORY_REPO_DIR": "",
            "FACTORY_BASE": base,
            "FACTORY_SETUP": command,
        }
        if settings.github_token:
            env["GH_TOKEN"] = settings.github_token
        exit_code = await asyncio.wait_for(
            _stream(boxd, machine.id, SCRIPT, env, run_log), timeout=settings.run_timeout
        )
        await db.update_run(run_id, exit_code=exit_code)
        if exit_code != 0:
            raise RuntimeError(f"setup exited {exit_code}")

        # Capture before the machine is destroyed, obviously — but also before anything else
        # can go wrong: everything the snapshot is for is on disk by now.
        run_log.write(f"[factory] capturing {snapshot}")
        await boxd.snapshots.create(machine.id, snapshot)
        # The fleet listing is memoised for CACHE_TTL, and the next dispatch should resolve
        # onto what was just built rather than onto the base for another ten seconds.
        agents.forget()

        await repos.record_golden(repo, snapshot, READY)
        await db.update_run(run_id, status="succeeded", finished_at=db.utcnow())
        run_log.write(f"[factory] {snapshot} saved; {repo}'s runs now boot it")
    finally:
        if machine is not None:
            try:
                await boxd.machines.delete(machine.id)
                run_log.write(f"[factory] destroyed {machine.name}")
            except Exception as exc:  # noqa: BLE001 - already gone is fine; a leak is not fatal
                run_log.write(f"[factory] could not destroy {machine.name}: {exc!r}")
        await boxd.close()


async def unprovision(repo: str) -> bool:
    """Delete `repo`'s golden, so its runs fall back to the base. Returns whether one went.

    The repair for a warm golden that has gone wrong in a way re-provisioning would inherit,
    and the thing to do before disconnecting a repo for good. Deliberately not automatic on
    disconnect: a snapshot is cheap to keep and slow to rebuild, and a repo is often
    reconnected.
    """
    snapshot = agents.golden_name(repo)
    if snapshot == agents.BASE_SNAPSHOT:
        raise ValueError(
            f"{repo!r} names no golden of its own — it resolves to {agents.BASE_SNAPSHOT}, "
            "which every repo boots. Refusing to delete it."
        )
    boxd = runner.client()
    try:
        await boxd.snapshots.delete(snapshot)
        removed = True
    except Exception as exc:  # noqa: BLE001 - "no such snapshot" is the ordinary case
        log.info("no %s to delete: %r", snapshot, exc)
        removed = False
    finally:
        await boxd.close()
    agents.forget()
    await repos.record_golden(repo, None, NONE)
    return removed
