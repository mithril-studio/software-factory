"""The bits of the GitHub API the control plane needs.

Deliberately tiny: fetch an issue, list issues, find the PR an agent opened. Everything
else the agent does for itself with `gh` inside the VM.
"""

from __future__ import annotations

import httpx

from .config import settings

API = "https://api.github.com"


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
