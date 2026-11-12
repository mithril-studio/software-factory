"""The bits of the GitHub API the control plane needs.

Fetch an issue, list the queue, find the PR an agent opened, make sure the target branch
exists, and mirror a run's lifecycle back onto the issue as labels and comments. Everything
else the agent does for itself with `gh` inside the VM.

Labels are a mirror of run state for humans reading GitHub, never the source of truth — that
lives in the `runs` table. So every label or comment write is best-effort at the call site.
"""

from __future__ import annotations

import base64

import httpx

from .config import settings

API = "https://api.github.com"
GIT = "https://github.com"

LABEL_QUEUED = "agent:queued"
LABEL_RUNNING = "agent:running"
LABEL_DONE = "agent:done"
LABEL_FAILED = "agent:failed"

LIFECYCLE_LABELS = {
    LABEL_QUEUED: ("8b93a3", "Factory: waiting to be picked up"),
    LABEL_RUNNING: ("e0af68", "Factory: an agent is working on this"),
    LABEL_DONE: ("4ec9a5", "Factory: finished, pull request opened"),
    LABEL_FAILED: ("f2777a", "Factory: run failed, re-add agent:queued to retry"),
}


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _api_error(resp) -> str:
    """GitHub's own explanation of a refusal, which `raise_for_status()` throws away."""
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 - a body we cannot parse is still worth quoting
        body = None
    parts: list[str] = []
    if isinstance(body, dict):
        parts.append(str(body.get("message") or "").strip())
        parts += [
            str(e.get("message")).strip()
            for e in body.get("errors") or []
            if isinstance(e, dict) and e.get("message")
        ]
    if not any(parts):
        parts = [(getattr(resp, "text", "") or "").strip()[:200]]
    needs = (getattr(resp, "headers", None) or {}).get("x-accepted-github-permissions")
    if needs:
        parts.append(f"needs {needs}")
    return "; ".join(p for p in parts if p)


# --------------------------------------------------------------------------- issues


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
        "state": data.get("state") or "open",
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


async def list_issues_with_label(repo: str, label: str, limit: int = 100) -> list[dict]:
    """Open issues carrying `label`, lowest number first — the poller's work queue.

    Asked oldest-first: GitHub's default order is newest-first, so with more than one page of
    queued issues the first page would not contain the lowest number.
    """
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            f"{API}/repos/{repo}/issues",
            headers=_headers(),
            params={
                "state": "open",
                "labels": label,
                "per_page": limit,
                "sort": "created",
                "direction": "asc",
            },
        )
        resp.raise_for_status()
        data = resp.json()
    issues = [
        {"number": i["number"], "title": i.get("title") or ""}
        for i in data
        if "pull_request" not in i
    ]
    return sorted(issues, key=lambda i: i["number"])


def _factory_state(labels: list[str]) -> str:
    """The factory lifecycle state for an issue, read off its labels. 'none' if untracked."""
    for label, state in (
        (LABEL_RUNNING, "running"),
        (LABEL_QUEUED, "queued"),
        (LABEL_FAILED, "failed"),
        (LABEL_DONE, "done"),
    ):
        if label in labels:
            return state
    return "none"


async def plan(repo: str, limit: int = 100) -> list[dict]:
    """Open issues for a repo with their factory state — the queue view, lowest first."""
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            f"{API}/repos/{repo}/issues",
            headers=_headers(),
            params={"state": "open", "per_page": limit, "sort": "created", "direction": "asc"},
        )
        resp.raise_for_status()
        data = resp.json()
    issues = []
    for i in data:
        if "pull_request" in i:
            continue
        labels = [label["name"] for label in i.get("labels", [])]
        issues.append(
            {
                "repo": repo,
                "number": i["number"],
                "title": i.get("title") or "",
                "url": i.get("html_url") or "",
                "state": _factory_state(labels),
                "labels": [label for label in labels if not label.startswith("agent:")],
            }
        )
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
    """Create any missing lifecycle labels. Idempotent: an existing label 422s, which is fine."""
    async with httpx.AsyncClient(timeout=20) as client:
        for name, (color, description) in LIFECYCLE_LABELS.items():
            resp = await client.post(
                f"{API}/repos/{repo}/labels",
                headers=_headers(),
                json={"name": name, "color": color, "description": description},
            )
            if resp.status_code not in (201, 422):
                resp.raise_for_status()


# --------------------------------------------------------------------------- pull requests


async def latest_pr(repo: str, branch: str) -> dict | None:
    """The newest pull request whose head is `branch`, open or closed, or None if it never had one.

    `{"url", "state": "open"|"closed", "draft", "head_sha"}`. Raises on any GitHub error rather
    than answering None: "no PR" decides whether a branch is continued or deleted, so an outage
    must never be read as one.
    """
    owner = repo.split("/")[0]
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            f"{API}/repos/{repo}/pulls",
            headers=_headers(),
            params={"head": f"{owner}:{branch}", "state": "all", "per_page": 1},
        )
        resp.raise_for_status()
        data = resp.json()
    if not data:
        return None
    pr = data[0]
    return {
        "url": pr.get("html_url") or "",
        "state": pr.get("state") or "closed",
        "draft": bool(pr.get("draft")),
        "head_sha": (pr.get("head") or {}).get("sha"),
    }


async def open_pr_heads(repo: str, base: str, prefix: str = "") -> list[str]:
    """Head branch names of the open pull requests into `base`, filtered by `prefix`."""
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            f"{API}/repos/{repo}/pulls",
            headers=_headers(),
            params={"state": "open", "base": base, "per_page": 100},
        )
        resp.raise_for_status()
        data = resp.json()
    heads = [(pr.get("head") or {}).get("ref") or "" for pr in data]
    return [h for h in heads if h and h.startswith(prefix)]


# --------------------------------------------------------------------------- repos and branches


async def file(repo: str, path: str, ref: str) -> str | None:
    """One file's raw contents from `repo` at `ref`. None when it is not there."""
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            f"{API}/repos/{repo}/contents/{path}",
            headers={**_headers(), "Accept": "application/vnd.github.raw"},
            params={"ref": ref},
            follow_redirects=True,
        )
    if resp.status_code != 200:
        return None
    return resp.text


async def repo_info(repo: str) -> dict | None:
    """The repository object, or None when the token cannot even read it."""
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(f"{API}/repos/{repo}", headers=_headers())
    return resp.json() if resp.status_code == 200 else None


async def default_branch(repo: str) -> str:
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(f"{API}/repos/{repo}", headers=_headers())
        resp.raise_for_status()
    return resp.json().get("default_branch", "main")


async def list_repos(limit: int = 300) -> list[dict]:
    """Every repo this token can see, most recently pushed first. For the connect picker."""
    out: list[dict] = []
    async with httpx.AsyncClient(timeout=20) as client:
        for page in range(1, limit // 100 + 2):
            resp = await client.get(
                f"{API}/user/repos",
                headers=_headers(),
                params={
                    "affiliation": "owner,collaborator,organization_member",
                    "sort": "pushed",
                    "per_page": 100,
                    "page": page,
                },
            )
            resp.raise_for_status()
            body = resp.json()
            if not body:
                break
            out.extend(
                {
                    "full_name": r.get("full_name") or "",
                    "private": bool(r.get("private")),
                    "archived": bool(r.get("archived")),
                    "default_branch": r.get("default_branch") or "",
                    "pushed_at": r.get("pushed_at"),
                    "can_push": bool((r.get("permissions") or {}).get("push")),
                }
                for r in body
                if r.get("full_name")
            )
            if len(body) < 100 or len(out) >= limit:
                break
    return out[:limit]


async def ref_sha(repo: str, ref: str) -> str | None:
    """The commit `ref` points at right now, or None when it cannot be resolved. Never raises."""
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                f"{API}/repos/{repo}/commits/{ref}",
                headers={**_headers(), "Accept": "application/vnd.github.sha"},
            )
            resp.raise_for_status()
        return resp.text.strip() or None
    except Exception:  # noqa: BLE001 - attribution is observability, never a dispatch gate
        return None


async def branch_sha(repo: str, branch: str) -> str | None:
    """The commit `branch` points at, None when the branch does not exist. Raises otherwise."""
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(f"{API}/repos/{repo}/git/ref/heads/{branch}", headers=_headers())
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()["object"]["sha"]


async def create_branch(repo: str, branch: str, sha: str) -> None:
    """Create `branch` at `sha`."""
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"{API}/repos/{repo}/git/refs",
            headers=_headers(),
            json={"ref": f"refs/heads/{branch}", "sha": sha},
        )
    if resp.status_code >= 400:
        raise httpx.HTTPStatusError(
            f"could not create {branch}: {resp.status_code} {_api_error(resp)}",
            request=resp.request,
            response=resp,
        )


async def can_push(repo: str) -> tuple[bool, str]:
    """Whether this token may actually push to `repo` — asked of git, not of metadata.

    `permissions.push` on the repo object describes the account, not the token, so a
    read-only token reads green there. The `git-receive-pack` handshake answers in the terms a
    push is enforced in, and changes nothing. Never raises.
    """
    if not settings.github_token:
        return False, "no GitHub token is configured, so nothing can push"
    auth = base64.b64encode(f"x-access-token:{settings.github_token}".encode()).decode()
    headers = {"Authorization": f"Basic {auth}", "User-Agent": "git/2.40"}
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(
                f"{GIT}/{repo}.git/info/refs",
                params={"service": "git-receive-pack"},
                headers=headers,
            )
    except httpx.TransportError as exc:
        return False, f"could not reach {GIT} to ask: {exc!r}"
    if resp.status_code == 200:
        return True, "git-receive-pack accepts this token"
    if resp.status_code in (401, 403):
        return False, (
            f"git-receive-pack refused this token ({resp.status_code}) — it can read {repo} "
            "but not write to it, so a run would clone, work, and die at the push"
        )
    return False, f"git-receive-pack answered {resp.status_code}, which is neither yes nor no"
