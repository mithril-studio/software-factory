"""Is this repo ready for the factory?

Every check here answers a question that otherwise gets answered by a run: forty minutes, a
VM, and an agent's context window spent discovering that the token cannot push, or that
nothing on GitHub will ever report a check run so the pull request can never merge. Cheap to
ask now, expensive to learn later.

It used to ask half its questions of the golden, over an `exec`: is the checkout the right
repo, is it clean, is it on the base branch, how far behind, does node match `.nvmrc`. A
golden is a snapshot now and there is no machine to `exec` into, so five of those were about
a thing that cannot be asked and the sixth — the toolchain — is what a run's own setup step
installs. What is left of the machine half is one question that costs no VM at all: is there
a golden for this repo's runs to boot, its own or the base image behind it.

It inspects, it does not repair — what it reports is what a human then fixes, because a
preflight that quietly repairs things is one nobody can trust the verdict of. The single
exception is creating the lifecycle labels, which is idempotent and which the poller does on
its own first sight of a repo anyway.

    .venv/bin/python -m control.preflight mithril-studio/legal-ai-app
"""

from __future__ import annotations

import asyncio
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass

from . import agents, github, runner
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


# A `## Setup` heading in the profile, at any level. What follows it is the command the
# prompt now tells the agent to run before it touches any code; nothing here reads that
# command, only whether the repo bothered to name one.
SETUP_SECTION = re.compile(r"^#{1,6}\s*set[ -]?up\b", re.I | re.M)


def profile_checks(profile: str | None) -> list[Check]:
    """What the repo's `.factory.md` says, and the one thing it most needs to say.

    Both findings are warnings, deliberately. A repo with no profile still builds and a
    profile with no setup section still helps; what they cost is turns, not the run — the
    agent works the install out from the lock file instead of being told. Blocking on it
    would stop onboarding over something no run needs.
    """
    text = (profile or "").strip()
    if not text:
        return [
            Check(
                f"has {runner.PROFILE_PATH}",
                False,
                "the agent gets a generic default without it",
                fatal=False,
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
    """Read `github.can_push`'s answer as a check.

    Split from the request so the verdict is testable without credentials, the same way
    `golden_check` takes the fleet rather than fetching it. Blocking either way: a token that
    cannot write is not a run that goes worse, it is a run that cannot finish — it clones,
    spends an agent's whole context, and dies at the push with a branch that never existed.

    "Could not tell" is also blocking, and deliberately. The failure this replaces was a check
    that answered `ok` without ever asking the question; anything less than a yes should read
    as a no.
    """
    return Check("token can push", ok, detail)


def golden_check(repo: str, available: Iterable[str] = ()) -> Check:
    """Is there a golden snapshot for this repo's runs to boot?

    The cheapest question the old VM probe was standing in for, and the only one of them still
    worth asking: a repo that resolves to nothing does not fail slowly, it fails at the first
    dispatch, having spent nothing.

    Which is a narrower failure than it used to be. A repo with no warm golden of its own is
    fine and says so — it boots `golden-copy` and installs for itself, so connecting a repo
    never waits on provisioning. The only blocking answer is a deployment with no base image
    at all, which is not this repo's problem to fix and would stop every other repo too.

    `available` is passed in rather than fetched so the decision is testable without
    credentials; `run()` is the one place that talks to the fleet.
    """
    snapshot = agents.resolve_snapshot(repo, available)
    if snapshot == agents.golden_name(repo):
        return Check("a golden is resolvable", True, f"{snapshot}, warmed for this repo")
    if snapshot:
        return Check(
            "a golden is resolvable",
            True,
            f"{snapshot} — no warm golden for {repo} yet, so its runs clone and install for "
            "themselves. A speed-up to provision, not a blocker.",
        )
    fleet = ", ".join(sorted(available)) or "no golden snapshots at all"
    return Check(
        "a golden is resolvable",
        False,
        f"no {agents.BASE_SNAPSHOT} to fall back on; the fleet holds {fleet}",
    )


async def _repo_checks(repo: str, base: str) -> list[Check]:
    """What only GitHub can answer."""
    info = await github.repo_info(repo)
    if info is None:
        return [Check("repo readable", False, f"the token cannot read {repo}")]
    checks = [
        Check("repo readable", True, f"default branch {base}"),
        # The agent pushes its branch and opens the pull request as this token. Asked of git
        # rather than of `info["permissions"]`, which answers for the account and not the
        # token — see `github.can_push`.
        push_check(*await github.can_push(repo)),
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
    checks.extend(profile_checks(await github.file(repo, runner.PROFILE_PATH, base)))
    try:
        await github.ensure_labels(repo)
        checks.append(Check("lifecycle labels", True, "present or created"))
    except Exception as exc:  # noqa: BLE001 - the message is the finding
        checks.append(Check("lifecycle labels", False, str(exc)))
    return checks


async def _fleet() -> tuple[tuple[str, ...], Check | None]:
    """The golden snapshot names, plus a failing check when the fleet could not be read.

    Kept apart from `golden_check` on purpose: "boxd is unreachable" and "there is no base
    image" are different repairs, and an outage reported as a missing golden sends whoever
    reads it to build a snapshot that is already there.
    """
    boxd = runner.client()
    try:
        return await agents.available(boxd), None
    except Exception as exc:  # noqa: BLE001 - the message is the finding
        return (), Check("fleet readable", False, f"could not list snapshots: {exc}")
    finally:
        await boxd.close()


async def run(repo: str) -> list[Check]:
    """Every check for `repo`, and for the golden its runs would boot."""
    base, (available, outage) = await asyncio.gather(
        github.default_branch(repo), _fleet()
    )
    checks = await _repo_checks(repo, base)
    if outage:
        checks.append(outage)
    else:
        checks.append(golden_check(repo, available))
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
