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
    """Env first, then the local gh CLI, so local development works with zero setup."""
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
    """Parse FACTORY_REPOS into repo names: the seed for the repo register.

    A legacy `owner/repo=agent` entry still parses; the agent half is dropped.
    """
    raw = os.environ.get("FACTORY_REPOS", "")
    out = []
    for entry in raw.split(","):
        repo, _, _legacy_agent = entry.partition("=")
        repo = repo.strip()
        if repo:
            out.append(repo)
    return tuple(out)


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    boxd_api_key: str = _env("BOXD_API_KEY", "")
    github_token: str = _github_token()
    anthropic_api_key: str = _env("ANTHROPIC_API_KEY", "")
    claude_code_oauth_token: str = _env("CLAUDE_CODE_OAUTH_TOKEN", "")

    # Where every pull request goes. Created from the repo's default branch if missing.
    # A repo can override it in the register (`repos.base_branch`).
    target_branch: str = _env("FACTORY_TARGET_BRANCH", "test")

    # Wait for the factory's open PR into the target branch to be merged or closed before
    # starting the next issue, so dependent issues build on each other. 0 for independent ones.
    sequential: bool = _env("FACTORY_SEQUENTIAL", "1") == "1"

    # Every VM this control plane creates starts with this. The reconciler only ever touches
    # machines carrying it, so two control planes on one boxd account must use different ones.
    vm_prefix: str = _env("FACTORY_VM_PREFIX", "job-")

    # Soft budget per run. Crossing it flags the run (UI, issue comment); it does not stop it.
    run_budget_eur: float = float(_env("FACTORY_RUN_BUDGET_EUR", "100"))
    # Telemetry prices runs in USD. Approximate, and only used for the budget flag.
    usd_per_eur: float = float(_env("FACTORY_USD_PER_EUR", "1.15"))

    workdir: str = _env("FACTORY_WORKDIR", "")
    max_concurrent: int = int(_env("FACTORY_MAX_CONCURRENT", "3"))
    run_timeout: int = int(_env("FACTORY_RUN_TIMEOUT", "5400"))
    bash_default_timeout: int = int(_env("FACTORY_BASH_TIMEOUT", "600"))
    bash_max_timeout: int = int(_env("FACTORY_BASH_MAX_TIMEOUT", "1800"))
    auto_destroy: int = int(_env("FACTORY_AUTO_DESTROY", "7200"))
    max_machines: int = int(_env("FACTORY_MAX_MACHINES", "20"))
    reconcile_interval: int = int(_env("FACTORY_RECONCILE_INTERVAL", "300"))
    keep_failed: bool = _env("FACTORY_KEEP_FAILED", "0") == "1"
    agent_effort: str = _env("FACTORY_AGENT_EFFORT", "medium")

    repos: tuple[str, ...] = _repos()
    poll_enabled: bool = _env("FACTORY_POLL", "1") == "1"
    poll_interval: int = int(_env("FACTORY_POLL_INTERVAL", "30"))
    log_level: str = _env("FACTORY_LOG_LEVEL", "INFO").upper()
    base_url: str = _env("FACTORY_BASE_URL", "").rstrip("/")
    db_path: Path = ROOT / "var" / "factory.db"
    log_dir: Path = ROOT / "var" / "logs"

    @property
    def run_budget_usd(self) -> float:
        return self.run_budget_eur * self.usd_per_eur

    def missing(self) -> list[str]:
        """Which required settings are absent. Surfaced in the UI rather than crashing."""
        gaps = []
        if not self.boxd_api_key:
            gaps.append("BOXD_API_KEY")
        if not self.github_token:
            gaps.append("GITHUB_TOKEN (or `gh auth login`)")
        return gaps

    def problems(self, available: Iterable[str] = ()) -> list[str]:
        """What this configuration cannot run, given the golden snapshots that exist."""
        if agents.BASE_SNAPSHOT in tuple(available or ()):
            return []
        return [
            f"no {agents.BASE_SNAPSHOT} snapshot — every run boots it unless the repo has a "
            "warm golden of its own, so nothing can dispatch without it"
        ]


settings = Settings()
settings.log_dir.mkdir(parents=True, exist_ok=True)
settings.db_path.parent.mkdir(parents=True, exist_ok=True)
