"""Configuration, read once from the environment at import time."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

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


@dataclass(frozen=True)
class RepoTarget:
    """A watched repo together with the machine its runs happen on.

    A run forks a golden and works in one checkout on it, so "which repo" and "which golden"
    are the same decision — a golden holding project A's checkout cannot build project B. One
    global golden therefore capped the whole factory at one project. Pairing them here is what
    lets FACTORY_REPOS list more than one.
    """

    repo: str
    golden: str
    repo_dir: str


def _targets() -> tuple[RepoTarget, ...]:
    """Parse FACTORY_REPOS: comma-separated `owner/repo[=golden[:repo_dir]]`.

    A bare `owner/repo` falls back to FACTORY_GOLDEN and FACTORY_REPO_DIR, so every existing
    single-project deployment keeps working unchanged.
    """
    raw = os.environ.get("FACTORY_REPOS", "")
    fallback_golden = os.environ.get("FACTORY_GOLDEN", "")
    fallback_dir = os.environ.get("FACTORY_REPO_DIR", "/home/boxd/repo")
    out = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        repo, _, machine = entry.partition("=")
        golden, _, repo_dir = machine.partition(":")
        out.append(
            RepoTarget(
                repo=repo.strip(),
                golden=golden.strip() or fallback_golden,
                repo_dir=repo_dir.strip() or fallback_dir,
            )
        )
    return tuple(out)


_TARGETS = _targets()


@dataclass(frozen=True)
class Settings:
    boxd_api_key: str = os.environ.get("BOXD_API_KEY", "")
    github_token: str = _github_token()
    golden: str = os.environ.get("FACTORY_GOLDEN", "")
    repo_dir: str = os.environ.get("FACTORY_REPO_DIR", "/home/boxd/repo")
    max_concurrent: int = int(os.environ.get("FACTORY_MAX_CONCURRENT", "3"))
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
    # Issue polling. The poller only runs if at least one repo is listed in FACTORY_REPOS.
    targets: tuple[RepoTarget, ...] = _TARGETS
    repos: tuple[str, ...] = tuple(t.repo for t in _TARGETS)
    poll_enabled: bool = os.environ.get("FACTORY_POLL", "1") == "1"
    poll_interval: int = int(os.environ.get("FACTORY_POLL_INTERVAL", "30"))
    # Public URL of this control plane, used only to link runs from issue comments.
    base_url: str = os.environ.get("FACTORY_BASE_URL", "").rstrip("/")
    db_path: Path = ROOT / "var" / "factory.db"
    log_dir: Path = ROOT / "var" / "logs"

    def target(self, repo: str) -> RepoTarget:
        """The golden and checkout a run for `repo` uses.

        An unwatched repo (a manual dispatch from the UI) falls back to the global golden, so
        dispatching something that is not in FACTORY_REPOS still behaves as it always did.
        """
        for t in self.targets:
            if t.repo == repo:
                return t
        return RepoTarget(repo=repo, golden=self.golden, repo_dir=self.repo_dir)

    def missing(self) -> list[str]:
        """Which required settings are absent. Surfaced in the UI rather than crashing."""
        gaps = []
        if not self.boxd_api_key:
            gaps.append("BOXD_API_KEY")
        if not self.github_token:
            gaps.append("GITHUB_TOKEN (or `gh auth login`)")
        # Per-repo goldens make the global one optional, but a target without any golden can
        # never run — name the repo so the fix is obvious.
        homeless = [t.repo for t in self.targets if not t.golden]
        if homeless:
            gaps.append(f"a golden for {', '.join(homeless)} (FACTORY_GOLDEN or FACTORY_REPOS)")
        elif not self.targets and not self.golden:
            gaps.append("FACTORY_GOLDEN")
        return gaps


settings = Settings()
settings.log_dir.mkdir(parents=True, exist_ok=True)
settings.db_path.parent.mkdir(parents=True, exist_ok=True)
