"""Is this repo ready for the factory, and is its golden ready for this repo?

Every check here answers a question that otherwise gets answered by a run: forty minutes, a
VM, and an agent's context window spent discovering that the checkout on the golden belongs
to a different project, or that nothing on GitHub will ever report a check run so the pull
request can never merge. Cheap to ask now, expensive to learn later.

It inspects, it does not repair — what it reports is what a human then fixes, because a
preflight that quietly repairs things is one nobody can trust the verdict of. The single
exception is creating the lifecycle labels, which is idempotent and which the poller does on
its own first sight of a repo anyway.

    .venv/bin/python -m control.preflight mithril-studio/legal-ai-app
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass

from . import github, probe, runner
from .config import settings


@dataclass(frozen=True)
class Check:
    """One question, its answer, and whether a `no` should stop the onboarding.

    `fatal=False` is for things that make runs worse rather than impossible — a repo with no
    `.factory.md` still builds, it just builds with less context.
    """

    name: str
    ok: bool
    detail: str
    fatal: bool = True

    @property
    def mark(self) -> str:
        return "ok  " if self.ok else ("FAIL" if self.fatal else "warn")


# One round trip instead of ten. Each line is `key=value`, read back by `probe.parse`.
VM_PROBE = r"""
cd "$REPO_DIR" 2>/dev/null || { echo "repo_dir=missing"; exit 0; }
echo "repo_dir=ok"
echo "origin=$(git config --get remote.origin.url 2>/dev/null | sed 's#.*[:/]\([^/]*/[^/]*\)$#\1#; s#\.git$##')"
echo "branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)"
echo "head=$(git rev-parse --short HEAD 2>/dev/null)"
echo "dirty=$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')"
git fetch --prune origin >/dev/null 2>&1 && echo "fetch=ok" || echo "fetch=failed"
echo "behind=$(git rev-list --count HEAD..origin/$BASE 2>/dev/null)"
echo "nvmrc=$([ -f .nvmrc ] && tr -dc '0-9.' < .nvmrc | cut -d. -f1 || echo none)"
echo "node=$(command -v node >/dev/null 2>&1 && node -v | tr -d 'v' | cut -d. -f1 || echo none)"
echo "claude=$(command -v claude >/dev/null 2>&1 && echo ok || echo missing)"
echo "skills=$(ls ~/.claude/skills 2>/dev/null | tr '\n' ' ')"
echo "gh=$(gh auth status >/dev/null 2>&1 && echo ok || echo missing)"
"""


async def _vm_checks(repo: str, golden: str, repo_dir: str, base: str) -> list[Check]:
    """What only the machine can answer. All of it from one `exec`."""
    boxd = runner.client()
    try:
        try:
            machine_id = await runner.golden_id(boxd, golden)
        except Exception as exc:  # noqa: BLE001 - the message is the finding
            return [Check("golden exists", False, f"{golden}: {exc}")]
        checks = [Check("golden exists", True, f"{golden} ({machine_id[:8]})")]
        result = await boxd.machines.exec(
            machine_id, command=VM_PROBE, env={"REPO_DIR": repo_dir, "BASE": base}, timeout=180
        )
    finally:
        await boxd.close()

    p = probe.parse(result.stdout)
    if p.get("repo_dir") != "ok":
        checks.append(Check("checkout present", False, f"{repo_dir} is not there on {golden}"))
        return checks
    checks.append(Check("checkout present", True, repo_dir))

    origin = p.get("origin", "")
    checks.append(
        Check(
            "checkout is this repo",
            origin.lower() == repo.lower(),
            f"origin is {origin or 'unset'}, expected {repo}",
        )
    )
    checks.append(
        Check("origin reachable", p.get("fetch") == "ok", f"git fetch {p.get('fetch', '?')}")
    )
    checks.append(
        Check(
            "on the base branch",
            p.get("branch") == base,
            f"on {p.get('branch') or '?'} at {p.get('head') or '?'}, base is {base}",
        )
    )
    # How stale the warm checkout is. Not fatal — a run checks out its own branch from
    # `origin/base` anyway — but it is what `node_modules` and the build cache were installed
    # against, so a golden far behind is one whose warmth has stopped being an advantage.
    behind = p.get("behind", "?")
    checks.append(
        Check(
            "golden is current",
            behind == "0",
            f"{behind} commit(s) behind origin/{base}",
            fatal=False,
        )
    )
    # A dirty golden is inherited by every fork, so uncommitted noise becomes something the
    # agent may commit without ever having touched it.
    checks.append(
        Check("checkout is clean", p.get("dirty") == "0", f"{p.get('dirty', '?')} modified file(s)")
    )
    # The mismatch that broke CI on fourteen consecutive commits, and the one VM_SCRIPT
    # already refuses to start on (exit 93) — better found here than there.
    want, have = p.get("nvmrc", "none"), p.get("node", "none")
    checks.append(
        Check(
            "toolchain matches .nvmrc",
            want == "none" or want == have,
            f".nvmrc pins {want}, machine runs node {have}",
        )
    )
    checks.append(Check("claude installed", p.get("claude") == "ok", p.get("claude", "?")))
    skills = p.get("skills", "")
    checks.append(
        Check("memory skill installed", "memory" in skills.split(), f"skills: {skills or 'none'}")
    )
    # The agent opens the pull request itself with `gh`; without this the run does all the
    # work and then has nowhere to put it.
    checks.append(Check("gh authenticated in the VM", p.get("gh") == "ok", p.get("gh", "?")))
    return checks


async def _repo_checks(repo: str, base: str) -> list[Check]:
    """What only GitHub can answer."""
    info = await github.repo_info(repo)
    if info is None:
        return [Check("repo readable", False, f"the token cannot read {repo}")]
    permissions = info.get("permissions", {})
    checks = [
        Check("repo readable", True, f"default branch {base}"),
        # The agent pushes its branch and opens the pull request as this token.
        Check("token can push", bool(permissions.get("push")), f"permissions: {permissions}"),
    ]
    # With no workflows, every pull request the factory opens waits out
    # FACTORY_MERGE_CHECK_TIMEOUT for a check run that never comes, and then stops for a
    # human — so this only blocks when the merge gate is actually switched on.
    count = await github.workflow_count(repo, base)
    checks.append(
        Check(
            "has CI workflows",
            count > 0,
            f"{count} workflow(s); with none, auto-merge can never pass its gate",
            fatal=settings.merge_require_checks and settings.auto_merge,
        )
    )
    profile = await github.file(repo, runner.PROFILE_PATH, base)
    checks.append(
        Check(
            f"has {runner.PROFILE_PATH}",
            bool(profile and profile.strip()),
            "the agent gets a generic default without it",
            fatal=False,
        )
    )
    try:
        await github.ensure_labels(repo)
        checks.append(Check("lifecycle labels", True, "present or created"))
    except Exception as exc:  # noqa: BLE001 - the message is the finding
        checks.append(Check("lifecycle labels", False, str(exc)))
    return checks


async def run(repo: str) -> list[Check]:
    """Every check for `repo`, against the golden its runs would actually fork."""
    golden = settings.golden_for(repo)
    if not golden:
        return [Check("golden configured", False, f"no golden for {repo} in FACTORY_REPOS")]
    base = await github.default_branch(repo)
    repo_checks, vm_checks = await asyncio.gather(
        _repo_checks(repo, base), _vm_checks(repo, golden, settings.repo_dir, base)
    )
    return repo_checks + vm_checks


def report(repo: str, checks: list[Check]) -> bool:
    """Print the checks. Returns whether the repo is safe to dispatch to."""
    print(f"preflight {repo}")
    for c in checks:
        print(f"  {c.mark}  {c.name}: {c.detail}")
    blocking = [c for c in checks if not c.ok and c.fatal]
    warnings = [c for c in checks if not c.ok and not c.fatal]
    print()
    if blocking:
        print(f"NOT READY — {len(blocking)} blocking: {', '.join(c.name for c in blocking)}")
    else:
        print("READY" + (f" — {len(warnings)} warning(s)" if warnings else ""))
    return not blocking


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python -m control.preflight owner/repo", file=sys.stderr)
        sys.exit(2)
    target = sys.argv[1]
    sys.exit(0 if report(target, asyncio.run(run(target))) else 1)
