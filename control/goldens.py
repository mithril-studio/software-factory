"""Watch the machines runs fork from, so staleness is visible instead of discovered.

A golden is warm on purpose: its dependencies are installed and its build cache is hot, and
the prompt tells the agent so — "do not reinstall, do not clear the build output". That
instruction is only true while the install still matches the repo. Nothing keeps it true, and
nothing said when it stopped being true. With one golden that is a chore somebody remembers;
with one per repo it is the thing that caps how many repos the factory can carry.

This sweep **observes**. It does not reset the checkout and does not install anything: a run
already checks its own branch out from `origin/<base>`, so the *code* on a golden is never
what goes stale — the *install* is, and how to redo that is project-specific (`npm ci`, `uv
sync`, something else). Guessing it here is how a control plane that contains no intelligence
starts containing some. What the sweep does instead is name the drift, including whether a
dependency manifest moved, so a human knows a golden needs rebuilding rather than finding out
from a run that installs 900 packages it was promised it already had.
"""

from __future__ import annotations

import asyncio
import logging

from . import db, github, probe, runner
from .config import settings

log = logging.getLogger("factory.goldens")

_task: asyncio.Task | None = None

# Files whose movement means the warm install is out of date. Deliberately a fixed list of
# manifests across ecosystems rather than anything clever: a name that is missing costs a
# missed warning, a name that is wrong costs a false one.
MANIFESTS = (
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "requirements.txt",
    "pyproject.toml",
    "uv.lock",
    "poetry.lock",
    "go.mod",
    "go.sum",
    "Gemfile.lock",
    "Cargo.lock",
)

PROBE = r"""
cd "$REPO_DIR" 2>/dev/null || { echo "error=repo dir $REPO_DIR not found"; exit 0; }
git fetch --prune origin >/dev/null 2>&1 || { echo "error=git fetch failed"; exit 0; }
echo "head=$(git rev-parse --short HEAD)"
echo "behind=$(git rev-list --count HEAD..origin/$BASE 2>/dev/null)"
echo "dirty=$(git status --porcelain | wc -l | tr -d ' ')"
echo "stale_deps=$(git diff --name-only HEAD "origin/$BASE" -- $MANIFESTS 2>/dev/null | tr '\n' ' ')"
echo "toolchain=$(command -v node >/dev/null 2>&1 && node -v || echo 'no node')"
"""


async def check(name: str, repo: str, repo_dir: str, base: str) -> dict:
    """Probe one golden. Returns the row recorded for it."""
    row: dict = {"repo": repo, "checked_at": db.utcnow(), "ok": 0}
    boxd = runner.client()
    try:
        machine_id = await runner.golden_id(boxd, name)
        result = await boxd.machines.exec(
            machine_id,
            command=PROBE,
            env={"REPO_DIR": repo_dir, "BASE": base, "MANIFESTS": " ".join(MANIFESTS)},
            timeout=180,
        )
        seen = probe.parse(result.stdout)
    except Exception as exc:  # noqa: BLE001 - an unreachable golden is a finding, not a crash
        row["error"] = f"{type(exc).__name__}: {exc}"
        await db.record_golden(name, **row)
        return row
    finally:
        await boxd.close()

    if seen.get("error"):
        row["error"] = seen["error"]
    else:
        row.update(
            ok=1,
            head_sha=seen.get("head"),
            behind=int(seen.get("behind") or 0),
            dirty=int(seen.get("dirty") or 0),
            stale_deps=seen.get("stale_deps", "").strip(),
            toolchain=seen.get("toolchain"),
        )
    await db.record_golden(name, **row)
    return row


async def sweep() -> dict[str, dict]:
    """Check every configured golden once. Never raises — one bad machine is one bad row."""
    results = {}
    for repo in settings.repos:
        golden = settings.golden_for(repo)
        if not golden:
            continue
        # A golden being forked right now is busy, and a `git fetch` on it is one more thing
        # competing with a run. Staleness keeps.
        if await db.has_active_run(repo):
            log.info("%s busy, skipping %s", repo, golden)
            continue
        try:
            base = await github.default_branch(repo)
            results[golden] = await check(golden, repo, settings.repo_dir, base)
        except Exception:  # one bad golden must not stop the sweep
            log.exception("sweep failed for %s", golden)
    for name, row in results.items():
        if row.get("error"):
            log.warning("%s: %s", name, row["error"])
        elif row.get("stale_deps"):
            log.warning(
                "%s: %s behind, dependencies moved (%s)", name, row["behind"], row["stale_deps"]
            )
        elif row.get("behind"):
            log.info("%s: %s commits behind", name, row["behind"])
    return results


async def _loop() -> None:
    log.info("sweeping goldens every %ss", settings.golden_sweep_interval)
    while True:
        await sweep()
        await asyncio.sleep(settings.golden_sweep_interval)


def start() -> None:
    """Launch the sweep loop, unless it is switched off or nothing is watched."""
    global _task
    if _task is not None or not settings.golden_sweep_interval or not settings.repos:
        return
    _task = asyncio.create_task(_loop())


async def stop() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except asyncio.CancelledError:
        pass
    _task = None
