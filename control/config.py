"""Configuration, read once from the environment at import time."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from . import agents

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Minimal .env loader so there is no extra dependency for a single feature."""
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


_load_dotenv()


def _github_token() -> str:
    """Env first, then the local gh CLI.

    The gh fallback makes local development work with zero setup. On a server there is no
    gh login, so GITHUB_TOKEN must be set there.
    """
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        return token
    try:
        out = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=10, check=False
        )
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _repos() -> tuple[str, ...]:
    """Parse FACTORY_REPOS into repo names.

    An entry is `owner/repo`. A legacy `owner/repo=agent` suffix is accepted and dropped: the
    right-hand side used to name the agent whose golden a repo booted, and nothing selects a
    snapshot by agent any more — `golden-<repo-slug>` falls back to `golden-copy`, and which
    agent that image runs is the image's own business. Accepted rather than rejected so an
    existing deployment's `.env` keeps working across the upgrade instead of silently watching
    nothing.

    This is the seed, not the register. `POST /api/repos` is how a repo is connected now; what
    is listed here is what a deployment already had before there was anywhere else to put it.
    """
    raw = os.environ.get("FACTORY_REPOS", "")
    out = []
    for entry in raw.split(","):
        repo, _, _legacy_agent = entry.partition("=")
        repo = repo.strip()
        if repo:
            out.append(repo)
    return tuple(out)


@dataclass(frozen=True)
class Settings:
    boxd_api_key: str = os.environ.get("BOXD_API_KEY", "")
    github_token: str = _github_token()
    # Where a run puts the checkout it clones. Empty means "let the VM decide", which is
    # `$HOME/work` — the golden knows its own home directory and the control plane does not.
    workdir: str = os.environ.get("FACTORY_WORKDIR", "")
    # A pre-clone checkout to reuse instead of cloning, honoured only when it holds the repo
    # the run was assigned. Goldens built before they became repo-agnostic carry exactly one
    # checkout at this path; it is the rollback for that migration and goes away with it.
    repo_dir: str = os.environ.get("FACTORY_REPO_DIR", "/home/boxd/repo")
    max_concurrent: int = int(os.environ.get("FACTORY_MAX_CONCURRENT", "3"))
    # How many goldens may be warmed at once. Its own budget rather than a share of
    # max_concurrent, because provisioning is slow, bursty (connecting three repos starts
    # three) and must never be able to starve the builds — which is what happened while both
    # took the same semaphore.
    max_provision: int = int(os.environ.get("FACTORY_MAX_PROVISION", "2"))
    # How long to wait for boxd to finish *writing* a snapshot before giving up on it.
    # `snapshots.create` returns when the capture is queued, and destroying the machine
    # underneath a queued capture aborts it permanently — see agents.wait_until_captured.
    # Twenty minutes is generous against an 8GB image; past it the half-written name is
    # deleted rather than left in the fleet as something no dispatch can resolve onto.
    capture_timeout: int = int(os.environ.get("FACTORY_CAPTURE_TIMEOUT", "1200"))
    # Seconds between asking the fleet whether that capture has landed. One list call, so it
    # can be frequent; the wait is minutes long and nobody should spend an extra one of them
    # on a snapshot that went ready right after the last poll.
    capture_poll: float = float(os.environ.get("FACTORY_CAPTURE_POLL", "5"))
    # Hard ceiling on one agent run. Observed successful runs took 27-53 minutes, so 60
    # minutes killed work that was still going somewhere (and did it silently). 90 leaves
    # real headroom; it must stay comfortably below FACTORY_AUTO_DESTROY, which is the VM's
    # own self-destruct. The right long-term ceiling is spend, not wall clock — see
    # backlog.md §8.
    run_timeout: int = int(os.environ.get("FACTORY_RUN_TIMEOUT", "5400"))
    # Seconds a single shell command may run before the agent's tooling backgrounds it, and
    # the ceiling it may request for one. Both are far above any legitimate command here
    # (the longest observed build was 153s) because a backgrounded command is what the agent
    # then waits on forever. A truly stuck command is caught by run_timeout instead.
    bash_default_timeout: int = int(os.environ.get("FACTORY_BASH_TIMEOUT", "600"))
    bash_max_timeout: int = int(os.environ.get("FACTORY_BASH_MAX_TIMEOUT", "1800"))
    auto_destroy: int = int(os.environ.get("FACTORY_AUTO_DESTROY", "7200"))
    # The boxd account's concurrent-machine cap. Checked before provisioning, because past it
    # boxd refuses the create and the run that finds out is the one that dies. It counts the
    # *whole* fleet — goldens still held as machines, a control plane, somebody's scratch VM —
    # which is what makes it different from max_concurrent, and why that alone never protected
    # anything. 0 switches the check off.
    max_machines: int = int(os.environ.get("FACTORY_MAX_MACHINES", "20"))
    # How often to sweep the fleet against the runs table, in seconds. 0 switches it off.
    # Five minutes: a leaked VM holds a slot until its two-hour self-destruct, and the sweep is
    # one list call plus a delete per orphan.
    reconcile_interval: int = int(os.environ.get("FACTORY_RECONCILE_INTERVAL", "300"))
    keep_failed: bool = os.environ.get("FACTORY_KEEP_FAILED", "0") == "1"
    # Claude auth injected into each run so the agent authenticates with a durable credential
    # instead of the golden's short-lived OAuth session (which expires and can't run
    # unattended). CLAUDE_CODE_OAUTH_TOKEN for a Claude subscription (from `claude setup-token`),
    # or ANTHROPIC_API_KEY for console/API billing. Whichever is set is passed through.
    anthropic_api_key: str = os.environ.get("ANTHROPIC_API_KEY", "")
    claude_code_oauth_token: str = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    # Reasoning effort for the agent. The dominant cost in a run is that every turn re-reads
    # the whole context, so spend scales with turn count and context size rather than with any
    # single call. `medium` buys most of `high`'s quality for materially fewer tokens; raise it
    # if issues start coming back underdone.
    agent_effort: str = os.environ.get("FACTORY_AGENT_EFFORT", "medium")
    # How often to re-list the golden snapshots, in seconds. 0 switches the refresh off.
    # Five minutes rather than the sweep's hour, because this is a list call and not a probe
    # on a machine that may be forking runs: what it costs is one request, and what it buys
    # is an agent built by hand being dispatchable within a poll or two of existing.
    agent_refresh_interval: int = int(os.environ.get("FACTORY_AGENT_REFRESH", "300"))
    # Auto-merge a run's PR on success, so the next issue in a sequential backlog branches
    # from a main that already contains the previous issue's work.
    auto_merge: bool = os.environ.get("FACTORY_AUTO_MERGE", "0") == "1"
    # Wait for the PR's check runs to pass before auto-merging. The merge API waits for
    # nothing on its own, so with this off a PR is merged seconds after it is opened and CI
    # reports on a commit that is already in main. Off is only for deliberately testing what
    # auto-merge does; on is the setting you want once that question is settled.
    merge_require_checks: bool = os.environ.get("FACTORY_MERGE_REQUIRE_CHECKS", "1") == "1"
    # How long to wait for those checks before giving up and leaving the PR open.
    merge_check_timeout: int = int(os.environ.get("FACTORY_MERGE_CHECK_TIMEOUT", "900"))
    # After a build run opens a PR, run a second agent that checks it against the issue's
    # acceptance criteria before anything merges. Only applies to issues that actually carry a
    # machine-readable criteria block — without one there is nothing to check, so the review is
    # skipped rather than guessed at.
    review_enabled: bool = os.environ.get("FACTORY_REVIEW", "1") == "1"
    # How many times a review may send an issue back for changes before it stops and asks for a
    # human. Two is deliberate: a third failure means the issue is wrong, not the code.
    max_review_cycles: int = int(os.environ.get("FACTORY_MAX_REVIEW_CYCLES", "2"))
    # Retry a failed issue up to this many attempts total (1 = no retry). Each attempt is
    # its own run, and the previous attempt's log is fed to the next as context.
    max_attempts: int = int(os.environ.get("FACTORY_MAX_ATTEMPTS", "3"))
    # In a sequential project a permanently-failed issue blocks the ones after it. When on,
    # the poller stops dispatching a repo that has any open `agent:failed` issue until a
    # human clears it. Turn off if a repo's issues are independent.
    halt_on_failure: bool = os.environ.get("FACTORY_HALT_ON_FAILURE", "1") == "1"
    # --- the improvement loop
    #
    # Periodically, an agent reads what the last N issues actually cost in rejections and
    # failures and proposes changes to the things that shape agent behaviour: a repo's skills,
    # its `.factory.md`, its `.mem/`. Off by default. It is the one part of this system that
    # edits its own inputs, so it starts switched off and is turned on per deployment.
    learn_enabled: bool = os.environ.get("FACTORY_LEARN", "0") == "1"
    # How many issues a repo must finish before its next learning run. Volume rather than a
    # clock: a repo that shipped nothing this week has produced no new evidence, and a
    # learning run over the same window it already read is spend with no possible finding.
    learn_every: int = int(os.environ.get("FACTORY_LEARN_EVERY", "5"))
    # How far back the digest looks. Wider than the trigger on purpose — a change is judged
    # against what happened before it, so the window has to contain both sides.
    learn_window_days: int = int(os.environ.get("FACTORY_LEARN_WINDOW_DAYS", "30"))
    # Proposals one learning run may file. Small deliberately. The scarce resource is not the
    # agent's ideas, it is the review capacity to check them and the context budget to hold
    # what gets merged — and a loop allowed ten changes a cycle would spend both on its own
    # output. Three forces the run to rank rather than to list.
    learn_max_proposals: int = int(os.environ.get("FACTORY_LEARN_MAX_PROPOSALS", "3"))
    # Whether a filed proposal is labelled `agent:queued` — which is what makes the factory
    # build it — or left as a plain issue for a human to trigger. Off means the loop still
    # runs, still reasons, still files fully-formed issues, and stops one step short of
    # changing anything. That is the setting to start on: read a cycle or two of proposals
    # before letting them merge into the repos the factory works on.
    learn_autoqueue: bool = os.environ.get("FACTORY_LEARN_AUTOQUEUE", "0") == "1"

    # Issue polling. The repos this deployment starts up watching — see `_repos()`.
    repos: tuple[str, ...] = _repos()
    poll_enabled: bool = os.environ.get("FACTORY_POLL", "1") == "1"
    poll_interval: int = int(os.environ.get("FACTORY_POLL_INTERVAL", "30"))
    # How much the control plane says about itself. Everything under the `factory.` loggers —
    # the poller's dispatches and halts, the golden refresh, the fleet reconciler — goes to
    # stdout, which systemd appends to var/uvicorn.log. DEBUG adds the libraries.
    log_level: str = os.environ.get("FACTORY_LOG_LEVEL", "INFO").upper()
    # Public URL of this control plane, used only to link runs from issue comments.
    base_url: str = os.environ.get("FACTORY_BASE_URL", "").rstrip("/")
    db_path: Path = ROOT / "var" / "factory.db"
    log_dir: Path = ROOT / "var" / "logs"

    def missing(self) -> list[str]:
        """Which required settings are absent. Surfaced in the UI rather than crashing.

        Static gaps only. Whether anything is *runnable* — whether a golden exists to boot —
        is a question for the fleet, and this is synchronous and gates `POST /api/runs`, so
        it must never grow an `await`. `problems()` answers that one.
        """
        gaps = []
        if not self.boxd_api_key:
            gaps.append("BOXD_API_KEY")
        if not self.github_token:
            gaps.append("GITHUB_TOKEN (or `gh auth login`)")
        return gaps

    def problems(self, available: Iterable[str] = ()) -> list[str]:
        """What this configuration cannot run, given the golden snapshots that exist.

        One question now, because there is one image to be missing. A repo with no golden of
        its own is not a problem — it boots `golden-copy` and installs for itself, which is
        the whole point of the fallback and is why connecting a repo never waits on
        provisioning. A deployment with no `golden-copy` has nothing to boot at all.

        Reported rather than raised: a missing snapshot is a thing to go build, not a broken
        setting, and the answer changes the moment somebody builds one.
        """
        if agents.BASE_SNAPSHOT in tuple(available or ()):
            return []
        return [
            f"no {agents.BASE_SNAPSHOT} snapshot — every run boots it unless the repo has a "
            "warm golden of its own, so nothing can dispatch without it"
        ]


settings = Settings()
settings.log_dir.mkdir(parents=True, exist_ok=True)
settings.db_path.parent.mkdir(parents=True, exist_ok=True)
