"""Warm a golden for one repo: restore the base, clone, install, capture, destroy.

A repo's golden is `golden-<repo-slug>` — the base image with that repo already cloned and, if
the repo says how, its dependencies installed. It is a **speed-up and nothing else**: a repo
without one dispatches onto `golden-copy` and installs for itself, which is what lets a repo be
connected and dispatched in the same minute. So everything here is allowed to be slow, allowed
to fail, and never allowed to block a run.

The install is the optional half. A repo that names no `## Setup` command still gets a golden
with the clone in it, because the clone is the part that can be done without guessing and it
is worth minutes on its own. What is never done is inferring an install command from a lock
file — see `create`.

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

## Waiting for the capture

`snapshots.create` returns once boxd has *queued* the capture, and destroying the machine
underneath a queued capture aborts it for good — the name is left `pending` with no version,
which `agents.available()` correctly refuses to dispatch onto and which nothing can ever
repair. That is how `golden-mithril-studio-software-factory` came to sit in the fleet at
`v0 / pending / 0B` while the register said the repo was warm. So the reap now happens after
`agents.wait_until_captured` says a new version has landed, and a capture that never lands is
deleted rather than left to look like progress.

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
if [ -n "$FACTORY_SETUP" ]; then
  echo "FACTORY: installing with: $FACTORY_SETUP"
  eval "$FACTORY_SETUP" || { echo "FACTORY: setup command failed" >&2; exit 94; }
  echo "FACTORY: install finished"
else
  echo "FACTORY: no setup command in .factory.md; capturing the clone without installing."
  echo "FACTORY: runs on this golden still install for themselves. Add a ## Setup section"
  echo "FACTORY: naming the command in backticks, then rebuild, to bake the install in too."
fi
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

    A repo that names no setup command still gets a golden — the clone, without the install.
    That is worth having on its own: every run of a repo with no warm snapshot clones it
    afresh, and the clone is the part the control plane can always do without guessing. What
    it is *not* allowed to do is invent an install command; a control plane with opinions
    about how projects install is how one project's command became every project's. So the
    install is skipped and the run log says so, rather than being inferred from a lock file.

    A command that spans more than one line is still refused. That is a malformed profile,
    not an absent one, and running it would be running something nobody wrote.
    """
    repo = repo.strip()
    base = await github.default_branch(repo)
    command = await setup_command(repo, base)
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


async def _surviving_golden(repo: str) -> str | None:
    """`repo`'s golden if one is still restorable, else `None`. Never raises.

    Asked on the failure paths because provisioning always rebuilds from the base, so a
    failed *re*-provision leaves the previous snapshot untouched and still bootable —
    `resolve_snapshot` will keep resolving onto it. Blanking the register in that case makes
    the Projects page report "no golden" for a repo whose runs are booting one, which sends
    whoever reads it to rebuild a snapshot that is fine.
    """
    snapshot = agents.golden_name(repo)
    if snapshot == agents.BASE_SNAPSHOT:
        return None
    boxd = runner.client()
    try:
        agents.forget()
        return snapshot if snapshot in await agents.available(boxd) else None
    except Exception:  # noqa: BLE001 - a fleet nobody can list answers nothing, not "gone"
        log.exception("could not check whether %s survived", snapshot)
        return None
    finally:
        await boxd.close()


async def _guarded(run_id: str, repo: str, base: str, command: str) -> None:
    run_log = runner.RunLog(Path(settings.log_dir / f"{run_id}.log"))
    try:
        # Its own semaphore, not the runs' one: a warm-up is allowed to be slow, and three of
        # them taking every build slot is a factory that has stopped.
        async with runner.provision_semaphore():
            await _execute(run_id, repo, base, command, run_log)
    except asyncio.CancelledError:
        run_log.write("[factory] provisioning cancelled")
        await db.update_run(run_id, status="cancelled", finished_at=db.utcnow())
        survivor = await _surviving_golden(repo)
        await repos.record_golden(repo, survivor, READY if survivor else NONE)
        raise
    except Exception as exc:  # noqa: BLE001 - a failed warm-up is a slower repo, not an outage
        reason = f"crashed: {str(exc)[:200] or type(exc).__name__}"
        run_log.write(f"[factory] provisioning failed: {exc!r}")
        await db.update_run(run_id, status="failed", error=reason, finished_at=db.utcnow())
        survivor = await _surviving_golden(repo)
        if survivor:
            run_log.write(f"[factory] {survivor} from the previous build is still bootable")
        await repos.record_golden(repo, survivor, READY if survivor else FAILED)
    finally:
        run_log.close()


async def _await_capture(boxd, snapshot: str, before: str, run_log: runner.RunLog) -> str:
    """Block until `snapshot` is restorable, and clean up after itself if it never is.

    `boxd.snapshots.create` returns once the capture is *queued*. Reaping the machine at that
    point aborts the capture, and the name is left in the fleet at `pending` with no version
    behind it — permanently unrestorable, and invisible to `agents.available()` so every run
    silently keeps booting the base while the register claims the repo is warm. That is not a
    hypothesis; it is what `golden-mithril-studio-software-factory` was found in.

    A capture that times out is deleted rather than left there, because a name with no version
    is worse than no name at all: it is the one shape `resolve_snapshot` cannot see and a
    person reading `snapshots list` cannot distinguish from progress.
    """
    try:
        return await agents.wait_until_captured(
            boxd, snapshot, before, settings.capture_timeout, settings.capture_poll
        )
    except TimeoutError:
        run_log.write(f"[factory] {snapshot} never finished capturing; deleting the fragment")
        try:
            await boxd.snapshots.delete(snapshot)
        except Exception as exc:  # noqa: BLE001 - best effort; the run has already failed
            run_log.write(f"[factory] could not delete {snapshot}: {exc!r}")
        agents.forget()
        raise


async def _execute(run_id: str, repo: str, base: str, command: str, run_log: runner.RunLog) -> None:
    snapshot = agents.golden_name(repo)
    vm_name = f"{runner.PROVISION_PREFIX}{run_id[:8]}"
    boxd = runner.client()
    machine = None
    try:
        await db.update_run(run_id, status="forking", started_at=db.utcnow(), golden=snapshot)
        run_log.write(f"[factory] warming {snapshot} for {repo} from {agents.BASE_SNAPSHOT}")
        await runner.headroom(boxd, run_log)
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
        #
        # What the version is *before* the capture is the thing that makes the wait below
        # correct on a rebuild, and it has to be read before `create` rather than after.
        before = await agents.captured_version(boxd, snapshot)
        run_log.write(f"[factory] capturing {snapshot}")
        await boxd.snapshots.create(machine.id, snapshot)
        run_log.write("[factory] waiting for boxd to finish writing the capture")
        version = await _await_capture(boxd, snapshot, before, run_log)
        # The fleet listing is memoised for CACHE_TTL, and the next dispatch should resolve
        # onto what was just built rather than onto the base for another ten seconds.
        agents.forget()

        await repos.record_golden(repo, snapshot, READY)
        await db.update_run(run_id, status="succeeded", finished_at=db.utcnow())
        run_log.write(f"[factory] {snapshot} {version} saved; {repo}'s runs now boot it")
    finally:
        # Never kept for inspection, whatever FACTORY_KEEP_FAILED says: this machine exists
        # only to be photographed, and a failed warm-up leaves nothing on it worth an SSH.
        await runner.reap(boxd, machine, run_log)
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
