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


def _watched() -> tuple[tuple[str, str], ...]:
    """Parse FACTORY_REPOS into (repo, agent) pairs.

    An entry is `owner/repo` or `owner/repo=agent`. The right-hand side names an *agent*,
    never a machine: which snapshot that agent boots from is derived from its name at
    dispatch time, so a repo warmed into `golden-<agent>--<repo-slug>` gets it automatically
    and a repo without one falls back to the bare agent image. The pairing is written next
    to the repo rather than in a second env var so the two cannot drift apart, and this is
    what keeps configuration free of per-agent variables: a fourth agent needs none.

    An entry with no `=agent` is left empty here and resolves through `agent_for`, so the
    default can change without rewriting every entry.
    """
    raw = os.environ.get("FACTORY_REPOS", "")
    out = []
    for entry in raw.split(","):
        repo, _, agent = entry.partition("=")
        repo, agent = repo.strip(), agent.strip()
        if repo:
            out.append((repo, agent))
    return tuple(out)


def unknown_agent(name: str, available: Iterable[str] = ()) -> str | None:
    """Why `name` cannot be dispatched, or None when it can.

    The API is the one place a typo should be loud, so this backs a 400 rather than a
    fallback: a hand-started run that silently ran on a different agent than the one asked
    for would be a lie told at the only moment somebody was watching. A bad *config* value
    is treated the other way round — it falls back and logs, because a poller that stops
    dispatching over one stale name is worse than one that keeps working.
    """
    found = agents.discover(available)
    if name.strip() in found:
        return None
    if not found:
        return f"unknown agent {name!r}: no golden snapshots exist to run it"
    return f"unknown agent {name!r}: known agents are {', '.join(found)}"


@dataclass(frozen=True)
class Settings:
    boxd_api_key: str = os.environ.get("BOXD_API_KEY", "")
    github_token: str = _github_token()
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
    # Each entry carries the agent its runs use — see _watched().
    watched: tuple[tuple[str, str], ...] = _watched()
    # The agent for a repo that names none. `claude` unless a deployment says otherwise.
    agent_default: str = os.environ.get("FACTORY_AGENT_DEFAULT", "").strip() or agents.DEFAULT_AGENT
    poll_enabled: bool = os.environ.get("FACTORY_POLL", "1") == "1"
    poll_interval: int = int(os.environ.get("FACTORY_POLL_INTERVAL", "30"))
    # Public URL of this control plane, used only to link runs from issue comments.
    base_url: str = os.environ.get("FACTORY_BASE_URL", "").rstrip("/")
    db_path: Path = ROOT / "var" / "factory.db"
    log_dir: Path = ROOT / "var" / "logs"

    @property
    def repos(self) -> tuple[str, ...]:
        """Just the repo names, in the order the poller works them."""
        return tuple(repo for repo, _ in self.watched)

    def agent_for(self, repo: str) -> str:
        """The agent that takes `repo`'s issues: its own if it named one, else the default."""
        for name, agent in self.watched:
            if name == repo and agent:
                return agent
        return self.agent_default

    def missing(self) -> list[str]:
        """Which required settings are absent. Surfaced in the UI rather than crashing.

        Static gaps only. Whether anything is *runnable* — whether the agents named here have
        snapshots — is a question for the fleet, and this is synchronous and gates
        `POST /api/runs`, so it must never grow an `await`. `problems()` answers that one.
        """
        gaps = []
        if not self.boxd_api_key:
            gaps.append("BOXD_API_KEY")
        if not self.github_token:
            gaps.append("GITHUB_TOKEN (or `gh auth login`)")
        return gaps

    def problems(self, available: Iterable[str] = ()) -> list[str]:
        """What this configuration cannot run, given the golden snapshots that exist.

        Reported rather than raised: a missing snapshot is a thing to go build, not a broken
        setting, and the answer changes the moment somebody builds one.
        """
        names = tuple(available or ())
        found = agents.discover(names)
        if not found:
            return ["no golden snapshots found — build one named golden-<agent>"]
        out = []
        for agent in dict.fromkeys(a for _, a in self.watched if a):
            if agent not in found:
                out.append(f"agent {agent!r} is configured but has no golden-{agent} snapshot")
        unconfigured = [repo for repo, agent in self.watched if not agent]
        if self.agent_default not in found and (unconfigured or not self.watched):
            out.append(
                f"no golden-{self.agent_default} snapshot, so a repo that names no agent "
                "has nothing to run on"
            )
        return out


settings = Settings()
settings.log_dir.mkdir(parents=True, exist_ok=True)
settings.db_path.parent.mkdir(parents=True, exist_ok=True)
