"""Deploy a promoted release to its repo's stable test server, deterministically.

The test server is a persistent boxd machine, `test-<slug>`, built once by
`scripts/provision-test-server.sh` and never touched by the reconciler (its name carries no run
prefix). It holds a checkout of the release branch and runs the app under systemd. A deploy is
a shell script on that machine and nothing else — no agent, no model, no judgement:

    refuse a dirty tree or the wrong branch → fast-forward to the release sha → install →
    build → migrate → restart → wait for health

and if any step after the fast-forward fails, put the checkout back where it was, rebuild,
restart and check health again. What each step runs is the repo's own `## Deploy` section in
`.factory.md` (`preflight.deploy_facts`), read at the release's own commit, so a release
deploys the way that version says it deploys.

Two things this does not do, deliberately. It never starts, creates or destroys the machine:
a test server that is missing or stopped fails the deploy for a human, because a factory that
resurrects servers is one that hides why they went away. And it cannot undo a migration — the
rollback is the checkout, and a schema change that shipped with a broken build stays shipped.
Say so in the release's error, and let a human decide.

Modelled as a run of `kind='deploy'`, for the same reason provisioning is: a streamed log, a
status, a row in the dispatch log. `vm_id` is left NULL on purpose — `runner.cancel` deletes a
run's VM by that id, and the VM here is the test server.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path

from . import agents, db, github, preflight, provision, runner
from .config import settings

log = logging.getLogger("factory.deploy")

# Exit codes the script ends with, and what each means for the release.
OK = 0
REFUSED = 95  # nothing was changed: dirty tree, wrong branch, fetch failed, not a fast-forward
BUILD_FAILED = 96  # install, build or migrate failed; the checkout was rolled back
UNHEALTHY = 97  # restart or health failed; the checkout was rolled back
ROLLBACK_FAILED = 98  # a step failed and so did putting it back: the test server is broken


def deploy_outcome(code: int) -> tuple[bool, str]:
    """What a deploy script's exit code means. Returns (deployed, why)."""
    return {
        OK: (True, "deployed and healthy"),
        REFUSED: (False, "refused before changing anything: the checkout was dirty, on the "
                         "wrong branch, or could not fast-forward to the release"),
        BUILD_FAILED: (False, "install, build or migrate failed; the checkout was rolled back "
                              "and the previous version restarted (a migration is not undone)"),
        UNHEALTHY: (False, "the new version did not come up healthy; the checkout was rolled "
                           "back and the previous version restarted"),
        ROLLBACK_FAILED: (False, "a step failed and the rollback failed too — the test server "
                                 "is not serving a known version and needs a human"),
    }.get(code, (False, f"the deploy script exited {code}"))


# POSIX, like every script here: boxd exec runs /bin/sh. Values arrive as environment
# variables and multi-word commands run through `eval`, as `provision.SCRIPT` does, so nothing
# the repo wrote is re-quoted on the way. Every step announces itself with `FACTORY-DEPLOY:` so
# the run log reads as the sequence it was.
DEPLOY_SCRIPT = r"""
say() { echo "FACTORY-DEPLOY: $*"; }
SUDO=""
[ "$(id -u)" = "0" ] || SUDO="sudo -n"

cd "$DEPLOY_PATH" 2>/dev/null || { say "no checkout at $DEPLOY_PATH"; exit 95; }
BEFORE=$(git rev-parse HEAD) || { say "$DEPLOY_PATH is not a git checkout"; exit 95; }
say "before $BEFORE"
branch=$(git rev-parse --abbrev-ref HEAD)
[ "$branch" = "$DEPLOY_BRANCH" ] || { say "on $branch, not $DEPLOY_BRANCH; refusing"; exit 95; }
[ -z "$(git status --porcelain --untracked-files=no)" ] || {
  say "the checkout has local changes; refusing"; git status --short; exit 95; }
say "fetch origin $DEPLOY_BRANCH"
git fetch origin "$DEPLOY_BRANCH" || { say "fetch failed"; exit 95; }
say "fast-forward to $DEPLOY_SHA"
git merge --ff-only "$DEPLOY_SHA" || { say "not a fast-forward from $BEFORE; refusing"; exit 95; }

# The toolchain the app's own launcher uses (scripts/provision-test-server.sh): fnm's node, at
# the version the release pins, made the default so the restarted service runs on it too.
if [ -x "$HOME/.local/share/fnm/fnm" ] || command -v fnm > /dev/null 2>&1; then
  export PATH="$HOME/.local/share/fnm:$PATH"
  eval "$(fnm env 2>/dev/null)" 2>/dev/null || true
  if [ -f .nvmrc ]; then
    fnm use --install-if-missing > /dev/null 2>&1 && fnm default "$(fnm current)" > /dev/null 2>&1 || true
  fi
fi

restart() {
  say "restart $DEPLOY_SERVICE"
  $SUDO systemctl restart "$DEPLOY_SERVICE"
}

healthy() {
  say "waiting up to ${DEPLOY_HEALTH_WAIT}s for $DEPLOY_HEALTH"
  waited=0
  while [ "$waited" -lt "$DEPLOY_HEALTH_WAIT" ]; do
    if curl -fsS -o /dev/null --max-time 5 "$DEPLOY_HEALTH"; then
      say "healthy after ${waited}s"
      return 0
    fi
    sleep 1
    waited=$((waited + 1))
  done
  say "not healthy after ${DEPLOY_HEALTH_WAIT}s"
  return 1
}

build() {
  say "install"
  eval "$DEPLOY_INSTALL" || return 1
  say "build"
  eval "$DEPLOY_BUILD" || return 1
  if [ -n "$DEPLOY_MIGRATE" ]; then
    say "migrate"
    eval "$DEPLOY_MIGRATE" || return 1
  fi
}

rollback() {
  say "rolling back to $BEFORE"
  git reset --hard "$BEFORE" || { say "rollback failed: reset"; exit 98; }
  eval "$DEPLOY_INSTALL" && eval "$DEPLOY_BUILD" || { say "rollback failed: rebuild"; exit 98; }
  restart || { say "rollback failed: restart"; exit 98; }
  healthy || { say "rollback failed: the previous version is not healthy either"; exit 98; }
  say "rolled back to $BEFORE"
  exit "$1"
}

build || rollback 96
restart || rollback 97
healthy || rollback 97
say "deployed $(git rev-parse HEAD)"
exit 0
"""


async def _facts_at(repo: str, sha: str) -> dict[str, str]:
    """The `## Deploy` section as the release's own commit has it."""
    return preflight.deploy_facts(await github.file(repo, runner.PROFILE_PATH, sha))


async def create(release_id: int) -> str:
    """Start deploying a promoted release. Returns the deploy run's id.

    Refuses with `ValueError` — and notes why on the release, which stays `promoted` — when
    the repo does not say how to deploy. Moving the release to `deploying` is the first write,
    so two callers racing to deploy the same release cannot both start one.
    """
    release = await db.get_release(release_id)
    if release is None:
        raise ValueError(f"no such release: {release_id}")
    if release["status"] != "promoted":
        raise ValueError(f"release {release_id} is {release['status']}, not promoted")
    repo, sha = release["repo"], release["test_sha"]
    facts = await _facts_at(repo, sha)
    problem = preflight.deploy_problem(facts)
    if problem:
        await db.set_release_error(release_id, f"not deployed: {problem}")
        raise ValueError(f"{repo} cannot be deployed: {problem}")

    await db.transition_release(release_id, "deploying", error=None)
    run_id = uuid.uuid4().hex
    log_path = settings.log_dir / f"{run_id}.log"
    log_path.touch()
    await db.create_run(
        id=run_id,
        repo=repo,
        issue_number=0,
        issue_title=f"deploy release {release_id} ({sha[:8]}) to {facts['machine']}",
        status="queued",
        kind="deploy",
        agent=agents.DEFAULT_AGENT,
        vm_name=facts["machine"],
        base_sha=sha,
        release_id=release_id,
        log_path=str(log_path),
        created_at=db.utcnow(),
    )
    runner.track(run_id, asyncio.create_task(_guarded(run_id, release_id, repo, sha, facts)))
    log.info("release %s: deploying %s %s to %s", release_id, repo, sha[:8], facts["machine"])
    return run_id


def deploy_env(sha: str, facts: dict[str, str]) -> dict[str, str]:
    """What the deploy script is told. One function so the test and the run agree on it."""
    return {
        "DEPLOY_PATH": facts["path"],
        "DEPLOY_BRANCH": settings.release_branch,
        "DEPLOY_SHA": sha,
        "DEPLOY_INSTALL": facts["install"],
        "DEPLOY_BUILD": facts["build"],
        "DEPLOY_MIGRATE": facts.get("migrate", ""),
        "DEPLOY_SERVICE": facts["service"],
        "DEPLOY_HEALTH": facts["health"],
        "DEPLOY_HEALTH_WAIT": str(settings.deploy_health_wait),
    }


async def _guarded(
    run_id: str, release_id: int, repo: str, sha: str, facts: dict[str, str]
) -> None:
    """Run the deploy and own both endings: the run row's and the release's."""
    run_log = runner.RunLog(Path(settings.log_dir / f"{run_id}.log"))
    try:
        deployed, why = await _execute(run_id, sha, facts, run_log)
        if deployed:
            await db.update_run(run_id, status="succeeded", finished_at=db.utcnow())
            await db.transition_release(
                release_id, "deployed",
                deployed_sha=sha, deployed_at=db.utcnow(), test_url=facts.get("url"),
            )
        else:
            await db.update_run(run_id, status="failed", error=why, finished_at=db.utcnow())
            await db.transition_release(release_id, "failed", error=f"deploy: {why}")
        run_log.write(f"[factory] {why}")
    except asyncio.CancelledError:
        run_log.write("[factory] deploy cancelled")
        await db.update_run(run_id, status="cancelled", finished_at=db.utcnow())
        await _fail_release(release_id, "deploy cancelled")
        raise
    except Exception as exc:  # noqa: BLE001 - a broken deploy is a failed release, not an outage
        reason = (
            f"timed out after {settings.deploy_timeout}s"
            if isinstance(exc, asyncio.TimeoutError)
            else f"{str(exc)[:300] or type(exc).__name__}"
        )
        run_log.write(f"[factory] deploy failed: {reason}")
        await db.update_run(run_id, status="failed", error=reason, finished_at=db.utcnow())
        await _fail_release(release_id, f"deploy: {reason}")
    finally:
        run_log.close()


async def _fail_release(release_id: int, error: str) -> None:
    try:
        await db.transition_release(release_id, "failed", error=error)
    except ValueError as exc:
        log.warning("release %s could not be failed: %s", release_id, exc)


async def _execute(
    run_id: str, sha: str, facts: dict[str, str], run_log: runner.RunLog
) -> tuple[bool, str]:
    """Find the test server, run the script on it, and say what the exit code means."""
    boxd = runner.client()
    try:
        await db.update_run(run_id, status="running", started_at=db.utcnow())
        name = facts["machine"]
        machine = next((m for m in await boxd.machines.list() if m.name == name), None)
        if machine is None:
            return False, f"no machine named {name} in the fleet; the factory never creates one"
        status = str(getattr(machine, "status", "") or "").lower()
        if status and status != "running":
            return False, f"{name} is {status}, not running; the factory never starts one"
        run_log.write(f"[factory] deploying {sha} to {name} ({machine.id})")
        code = await asyncio.wait_for(
            provision._stream(boxd, machine.id, DEPLOY_SCRIPT, deploy_env(sha, facts), run_log),
            timeout=settings.deploy_timeout,
        )
        await db.update_run(run_id, exit_code=code)
        return deploy_outcome(code)
    finally:
        await boxd.close()
