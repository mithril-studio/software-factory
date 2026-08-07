"""The bits of the GitHub API the control plane needs.

Deliberately tiny: fetch an issue, list issues, find the PR an agent opened, and mirror a
run's lifecycle back onto the issue as a label. Everything else the agent does for itself
with `gh` inside the VM.

The issue labels are a *mirror* of run state for humans reading GitHub, never the source of
truth — that lives in the `runs` table. So every write here is best-effort at the call site:
a label the API refuses must never fail an otherwise good run.
"""

from __future__ import annotations

import httpx

from .config import settings

API = "https://api.github.com"

# Lifecycle labels. One is meant to be present at a time; `agent:queued` is also the
# trigger — dropping it on an open issue is the whole contract for "please build this".
LABEL_QUEUED = "agent:queued"
LABEL_RUNNING = "agent:running"
LABEL_BLOCKED = "agent:blocked"
LABEL_DONE = "agent:done"
LABEL_FAILED = "agent:failed"

# name -> (hex color, description), used by ensure_labels to create them if absent.
LIFECYCLE_LABELS = {
    LABEL_QUEUED: ("8b93a3", "Factory: waiting to be picked up"),
    LABEL_RUNNING: ("e0af68", "Factory: an agent is working on this"),
    LABEL_BLOCKED: ("b48ead", "Factory: blocked, needs a human"),
    LABEL_DONE: ("4ec9a5", "Factory: finished, pull request opened"),
    LABEL_FAILED: ("f2777a", "Factory: run failed"),
}


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def get_issue(repo: str, number: int) -> dict:
    """Fetch one issue. Raises for status so the run fails loudly and early."""
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(f"{API}/repos/{repo}/issues/{number}", headers=_headers())
        resp.raise_for_status()
        data = resp.json()
    return {
        "number": data["number"],
        "title": data.get("title") or "",
        "body": data.get("body") or "",
        "url": data.get("html_url") or "",
        "labels": [label["name"] for label in data.get("labels", [])],
    }


async def list_open_issues(repo: str, limit: int = 30) -> list[dict]:
    """Open issues, pull requests filtered out (GitHub returns PRs from this endpoint too)."""
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            f"{API}/repos/{repo}/issues",
            headers=_headers(),
            params={"state": "open", "per_page": limit},
        )
        resp.raise_for_status()
        data = resp.json()
    return [
        {"number": i["number"], "title": i.get("title") or ""}
        for i in data
        if "pull_request" not in i
    ]


async def find_pr(repo: str, branch: str) -> str | None:
    """The PR whose head is `branch`, if the agent opened one."""
    owner = repo.split("/")[0]
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            f"{API}/repos/{repo}/pulls",
            headers=_headers(),
            params={"head": f"{owner}:{branch}", "state": "all", "per_page": 1},
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
    return data[0]["html_url"] if data else None


async def default_branch(repo: str) -> str:
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(f"{API}/repos/{repo}", headers=_headers())
        resp.raise_for_status()
    return resp.json().get("default_branch", "main")


async def list_issues_with_label(repo: str, label: str, limit: int = 100) -> list[dict]:
    """Open issues carrying `label`, lowest number first — the poller's work queue.

    Sorted ascending by number so "claim the lowest" is just the first element. Pull
    requests are filtered out; the issues endpoint returns them too.
    """
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            f"{API}/repos/{repo}/issues",
            headers=_headers(),
            params={"state": "open", "labels": label, "per_page": limit},
        )
        resp.raise_for_status()
        data = resp.json()
    issues = [
        {"number": i["number"], "title": i.get("title") or ""}
        for i in data
        if "pull_request" not in i
    ]
    return sorted(issues, key=lambda i: i["number"])


async def add_labels(repo: str, number: int, labels: list[str]) -> None:
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"{API}/repos/{repo}/issues/{number}/labels",
            headers=_headers(),
            json={"labels": labels},
        )
        resp.raise_for_status()


async def remove_label(repo: str, number: int, label: str) -> None:
    """Remove one label. A 404 (label wasn't there) is success, not an error."""
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.delete(
            f"{API}/repos/{repo}/issues/{number}/labels/{label}", headers=_headers()
        )
        if resp.status_code not in (200, 404):
            resp.raise_for_status()


async def add_comment(repo: str, number: int, body: str) -> None:
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"{API}/repos/{repo}/issues/{number}/comments",
            headers=_headers(),
            json={"body": body},
        )
        resp.raise_for_status()


async def ensure_labels(repo: str) -> None:
    """Create any missing lifecycle labels. Idempotent: an existing label 422s, which is fine.

    Called once per watched repo at startup so `add_labels` never fails on a fresh repo that
    has never seen the factory.
    """
    async with httpx.AsyncClient(timeout=20) as client:
        for name, (color, description) in LIFECYCLE_LABELS.items():
            resp = await client.post(
                f"{API}/repos/{repo}/labels",
                headers=_headers(),
                json={"name": name, "color": color, "description": description},
            )
            if resp.status_code not in (201, 422):
                resp.raise_for_status()
