"""Is this repo ready for the factory?

Each check answers a question that would otherwise be answered by a run: a VM and an agent's
whole context spent discovering that the token cannot push. It inspects and never repairs,
except for creating the lifecycle labels, which is idempotent.

    .venv/bin/python -m control.preflight owner/repo
"""

from __future__ import annotations

import asyncio
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass

from . import agents, github, repos, runner


@dataclass(frozen=True)
class Check:
    """One question, its answer, and whether a `no` should stop the onboarding."""

    name: str
    ok: bool
    detail: str
    fatal: bool = True

    @property
    def mark(self) -> str:
        return "ok  " if self.ok else ("FAIL" if self.fatal else "warn")


SETUP_SECTION = re.compile(r"^#{1,6}\s*set[ -]?up\b", re.I | re.M)
_BACKTICKED = re.compile(r"^.*`([^`]*)`")


def setup_command(profile: str | None) -> str:
    """The first backticked command under `## Setup` in a `.factory.md`, or `""`.

    `scripts/refresh-golden.sh` does the same walk in awk; `golden_scripts_test` pins the two
    in agreement.
    """
    started = False
    for line in (profile or "").splitlines():
        if not started:
            started = bool(SETUP_SECTION.match(line))
            continue
        if line.startswith("#"):
            break
        found = _BACKTICKED.match(line)
        if found:
            return found.group(1).strip()
    return ""


def profile_checks(profile: str | None) -> list[Check]:
    """Warnings only: a repo without a profile still builds, with less context."""
    text = (profile or "").strip()
    if not text:
        return [
            Check(
                f"has {runner.PROFILE_PATH}", False,
                "the agent gets a generic default without it", fatal=False,
            )
        ]
    return [
        Check(f"has {runner.PROFILE_PATH}", True, f"{len(text)} characters"),
        Check(
            f"{runner.PROFILE_PATH} names a setup command",
            bool(SETUP_SECTION.search(text)),
            "no `## Setup` section, so every run guesses how to install this repo",
            fatal=False,
        ),
    ]


def push_check(ok: bool, detail: str) -> Check:
    """Blocking either way: anything short of a yes is a run that dies at the push."""
    return Check("token can push", ok, detail)


def golden_check(repo: str, available: Iterable[str] = ()) -> Check:
    """Is there a golden for this repo's runs to boot? Only a missing base image blocks."""
    snapshot = agents.resolve_snapshot(repo, available)
    if snapshot == agents.golden_name(repo):
        return Check("a golden is resolvable", True, f"{snapshot}, warmed for this repo")
    if snapshot:
        return Check(
            "a golden is resolvable", True,
            f"{snapshot} — no warm golden for {repo}, so its runs clone and install themselves",
        )
    fleet = ", ".join(sorted(available)) or "no golden snapshots at all"
    return Check(
        "a golden is resolvable", False,
        f"no {agents.BASE_SNAPSHOT} to fall back on; the fleet holds {fleet}",
    )


async def _repo_checks(repo: str) -> list[Check]:
    info = await github.repo_info(repo)
    if info is None:
        return [Check("repo readable", False, f"the token cannot read {repo}")]
    default = info.get("default_branch") or "main"
    target = repos.target_branch(repo)
    exists = await github.ref_sha(repo, target)
    checks = [
        Check("repo readable", True, f"default branch {default}"),
        push_check(*await github.can_push(repo)),
        Check(
            "target branch", True,
            f"{target} at {exists[:8]}" if exists
            else f"{target} does not exist yet; the first run creates it from {default}",
        ),
    ]
    # Read the profile from where a run reads it: the target branch once it exists. Checking
    # the default branch warned "no .factory.md" about a repo whose runs had one.
    profile_ref = target if exists else default
    checks.extend(profile_checks(await github.file(repo, runner.PROFILE_PATH, profile_ref)))
    try:
        await github.ensure_labels(repo)
        checks.append(Check("lifecycle labels", True, "present or created"))
    except Exception as exc:  # noqa: BLE001 - the message is the finding
        checks.append(Check("lifecycle labels", False, str(exc)))
    return checks


async def _fleet() -> tuple[tuple[str, ...], Check | None]:
    """Golden names, plus a failing check when boxd could not be read at all."""
    boxd = runner.client()
    try:
        return await agents.available(boxd), None
    except Exception as exc:  # noqa: BLE001 - the message is the finding
        return (), Check("fleet readable", False, f"could not list snapshots: {exc}")
    finally:
        await boxd.close()


async def run(repo: str) -> list[Check]:
    """Every check for `repo`, and for the golden its runs would boot."""
    checks, (available, outage) = await asyncio.gather(_repo_checks(repo), _fleet())
    checks.append(outage or golden_check(repo, available))
    return checks


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
