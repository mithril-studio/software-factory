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


def _repos() -> tuple[str, ...]:
    """Repos the poller watches, from a comma-separated FACTORY_REPOS."""
    raw = os.environ.get("FACTORY_REPOS", "")
    return tuple(r.strip() for r in raw.split(",") if r.strip())


@dataclass(frozen=True)
class Settings:
    boxd_api_key: str = os.environ.get("BOXD_API_KEY", "")
    github_token: str = _github_token()
    golden: str = os.environ.get("FACTORY_GOLDEN", "")
    repo_dir: str = os.environ.get("FACTORY_REPO_DIR", "/home/boxd/repo")
    max_concurrent: int = int(os.environ.get("FACTORY_MAX_CONCURRENT", "3"))
    run_timeout: int = int(os.environ.get("FACTORY_RUN_TIMEOUT", "3600"))
    auto_destroy: int = int(os.environ.get("FACTORY_AUTO_DESTROY", "7200"))
    keep_failed: bool = os.environ.get("FACTORY_KEEP_FAILED", "0") == "1"
    # Injected into each run so the agent's `claude` authenticates with a durable API key
    # instead of the golden's OAuth session (which expires and can't run unattended).
    anthropic_api_key: str = os.environ.get("ANTHROPIC_API_KEY", "")
    # Auto-merge a run's PR on success, so the next issue in a sequential backlog branches
    # from a main that already contains the previous issue's work.
    auto_merge: bool = os.environ.get("FACTORY_AUTO_MERGE", "0") == "1"
    # Retry a failed issue up to this many attempts total (1 = no retry). Each attempt is
    # its own run, and the previous attempt's log is fed to the next as context.
    max_attempts: int = int(os.environ.get("FACTORY_MAX_ATTEMPTS", "3"))
    # In a sequential project a permanently-failed issue blocks the ones after it. When on,
    # the poller stops dispatching a repo that has any open `agent:failed` issue until a
    # human clears it. Turn off if a repo's issues are independent.
    halt_on_failure: bool = os.environ.get("FACTORY_HALT_ON_FAILURE", "1") == "1"
    # Issue polling. The poller only runs if at least one repo is listed in FACTORY_REPOS.
    repos: tuple[str, ...] = _repos()
    poll_enabled: bool = os.environ.get("FACTORY_POLL", "1") == "1"
    poll_interval: int = int(os.environ.get("FACTORY_POLL_INTERVAL", "30"))
    # Public URL of this control plane, used only to link runs from issue comments.
    base_url: str = os.environ.get("FACTORY_BASE_URL", "").rstrip("/")
    db_path: Path = ROOT / "var" / "factory.db"
    log_dir: Path = ROOT / "var" / "logs"

    def missing(self) -> list[str]:
        """Which required settings are absent. Surfaced in the UI rather than crashing."""
        gaps = []
        if not self.boxd_api_key:
            gaps.append("BOXD_API_KEY")
        if not self.github_token:
            gaps.append("GITHUB_TOKEN (or `gh auth login`)")
        if not self.golden:
            gaps.append("FACTORY_GOLDEN")
        return gaps


settings = Settings()
settings.log_dir.mkdir(parents=True, exist_ok=True)
settings.db_path.parent.mkdir(parents=True, exist_ok=True)
