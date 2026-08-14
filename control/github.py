"""The bits of the GitHub API the control plane needs.

Deliberately tiny: fetch an issue, list issues, find the PR an agent opened, and mirror a
run's lifecycle back onto the issue as a label. Everything else the agent does for itself
with `gh` inside the VM.

The issue labels are a *mirror* of run state for humans reading GitHub, never the source of
truth — that lives in the `runs` table. So every write here is best-effort at the call site:
a label the API refuses must never fail an otherwise good run.
"""

from __future__ import annotations

import asyncio

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


def _factory_state(labels: list[str]) -> str:
    """The factory lifecycle state for an issue, read off its labels. 'none' if untracked."""
    for label, state in (
        (LABEL_RUNNING, "running"),
        (LABEL_QUEUED, "queued"),
        (LABEL_BLOCKED, "blocked"),
        (LABEL_FAILED, "failed"),
        (LABEL_DONE, "done"),
    ):
        if label in labels:
            return state
    return "none"


async def plan(repo: str, limit: int = 100) -> list[dict]:
    """Open issues for a repo with their factory state — the Plan (work queue) view.

    Lowest number first, since that is the order the poller works through them.
    """
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


# A check run that ended in one of these did not fail. `neutral` and `skipped` are how a
# conditional job reports "not applicable here", which must not block a merge.
CHECK_OK = ("success", "neutral", "skipped")


async def pr_head_sha(repo: str, number: int) -> str:
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(f"{API}/repos/{repo}/pulls/{number}", headers=_headers())
        resp.raise_for_status()
    return resp.json()["head"]["sha"]


async def merge_base_sha(repo: str, base: str, head_sha: str) -> str:
    """The commit a branch actually diverged from.

    Not the current tip of `base`: main moves while a run works, and a reviewer that ran the
    branch's new tests against a *later* main would be testing someone else's changes too.
    The merge base is the code as it was when this work started, which is what "did this test
    fail before the change" has to mean. Falls back to the base branch name on any error —
    an approximate comparison beats none.
    """
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                f"{API}/repos/{repo}/compare/{base}...{head_sha}", headers=_headers()
            )
            resp.raise_for_status()
        return resp.json()["merge_base_commit"]["sha"]
    except Exception:  # noqa: BLE001
        return base


async def checks_green(
    repo: str, sha: str, timeout: int = 900, interval: int = 15
) -> tuple[bool, str, list[str]]:
    """Wait for every check run on `sha` to finish, and report whether all passed.

    Returns (ok, reason, failed). `ok` is True only when at least one check has run and all
    of them completed acceptably — an unverified commit is never treated as a green one.

    `failed` carries the names of checks that finished and did not pass, and is the caller's
    signal that the commit is *known* red rather than merely unmerged. It is empty for every
    other unhappy ending — still pending, timed out, API unreachable — because "we could not
    find out" and "CI says no" call for different responses: the first needs a human, the
    second is something an agent can be sent back to fix.

    This exists because the merge API does not wait for anything: without it, a PR is merged
    seconds after `gh pr create`, before CI has even started, and a red main then propagates
    into every subsequent run (which forks from main).

    Never raises. A GitHub hiccup returns ok=False so the caller leaves the PR open for a
    human rather than failing an otherwise good run.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    last = "no check runs reported"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            while True:
                resp = await client.get(
                    f"{API}/repos/{repo}/commits/{sha}/check-runs", headers=_headers()
                )
                resp.raise_for_status()
                runs = resp.json().get("check_runs", [])

                if not runs:
                    last = "no check runs reported"
                elif all(r.get("status") == "completed" for r in runs):
                    failed = [
                        f"{r.get('name')}={r.get('conclusion')}"
                        for r in runs
                        if r.get("conclusion") not in CHECK_OK
                    ]
                    if failed:
                        return False, f"checks failed: {', '.join(failed)}", failed
                    return True, f"{len(runs)} checks passed", []
                else:
                    pending = [r.get("name") for r in runs if r.get("status") != "completed"]
                    last = f"still running: {', '.join(p for p in pending if p)}"

                if asyncio.get_running_loop().time() >= deadline:
                    return False, f"timed out after {timeout}s ({last})", []
                await asyncio.sleep(interval)
    except Exception as exc:  # noqa: BLE001 - a merge we skip is always safer than one we force
        return False, f"could not read checks: {exc!r}", []


async def merge_pr(repo: str, number: int, method: str = "squash") -> dict:
    """Merge a pull request. Raises for status so a failed merge is loud.

    Squash by default: one clean commit per issue on the base branch, which is what the next
    issue in a sequential backlog branches from.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.put(
            f"{API}/repos/{repo}/pulls/{number}/merge",
            headers=_headers(),
            json={"merge_method": method},
        )
        resp.raise_for_status()
    return resp.json()


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
