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
import re

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


async def file(repo: str, path: str, ref: str) -> str | None:
    """Fetch one file's raw contents from `repo` at `ref`. None when it is not there.

    Used for a repo's `.factory.md` profile, which is why a missing file is an ordinary
    answer rather than an error: most repos will not have one, and the caller has a default.
    """
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


async def workflow_count(repo: str, ref: str) -> int:
    """How many Actions workflow files exist on `ref`.

    Zero is the interesting answer: `checks_green` reports success only once at least one
    check run has finished, so a repo with no workflows can never satisfy the merge gate.

    Counted from the tree rather than from `/actions/workflows`, which also lists workflows
    it has only ever seen on a branch — a pull request adding CI would otherwise make the
    repo look like it already had some.
    """
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            f"{API}/repos/{repo}/contents/.github/workflows",
            headers=_headers(),
            params={"ref": ref},
        )
    if resp.status_code != 200:
        return 0
    body = resp.json()
    return sum(1 for f in body if f.get("name", "").endswith((".yml", ".yaml")))


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


# GitHub was unavailable or throttling, rather than refusing. Everything else — 405 (not
# mergeable), 409 (head moved), 422 — is an answer, and answers are not retried.
TRANSIENT_STATUS = (429, 500, 502, 503, 504)


async def merge_pr(
    repo: str,
    number: int,
    sha: str | None = None,
    method: str = "squash",
    attempts: int = 4,
    backoff: float = 2.0,
) -> dict:
    """Merge a pull request, retrying for as long as GitHub is merely unavailable.

    Squash by default: one clean commit per issue on the base branch, which is what the next
    issue in a sequential backlog branches from.

    `sha` pins the commit. GitHub refuses with 409 if the head has moved since, so passing
    the sha the checks were verified on means a merge can never land a commit CI did not
    test. That pin is also what makes retrying safe: a retry cannot pick up a newer,
    unverified head while it is waiting.

    Retries exist because a transient outage used to be permanent. An approved pull request
    with every check green was stranded for a human because the merge call happened to hit a
    503 — the API came back a minute later, and nothing ever tried again.

    Raises for status once the retries are spent, so a failed merge is still loud.
    """
    payload: dict[str, str] = {"merge_method": method}
    if sha:
        payload["sha"] = sha

    delay, last = backoff, None
    async with httpx.AsyncClient(timeout=30) as client:
        for attempt in range(1, attempts + 1):
            try:
                resp = await client.put(
                    f"{API}/repos/{repo}/pulls/{number}/merge",
                    headers=_headers(),
                    json=payload,
                )
                if resp.status_code not in TRANSIENT_STATUS:
                    resp.raise_for_status()  # a refusal, not an outage - surface it now
                    return resp.json()
                last = httpx.HTTPStatusError(
                    f"merge API returned {resp.status_code}",
                    request=resp.request,
                    response=resp,
                )
            except httpx.TransportError as exc:  # connect/read timeouts and network errors
                last = exc
            if attempt == attempts:
                raise last
            await asyncio.sleep(delay)
            delay *= 2
    raise RuntimeError("unreachable")  # pragma: no cover


# GitHub Actions marks a failure with this, and puts the cause in the lines just above it.
_ERROR_MARK = "##[error]"
# Every log line is prefixed with an ISO timestamp we never read. At ~30 characters a line it
# is pure cache-read cost in the next agent's prompt, so it goes.
_LOG_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z\s?")


def extract_failure(log: str, max_chars: int = 4000, before: int = 40, after: int = 5) -> str:
    """The part of an Actions job log that says why the job failed.

    Taking the tail does not work: a job log ends with credential teardown and "Cleaning up
    orphan processes", so the last 4000 characters of a failed run are reliably the least
    informative 4000 characters in it. The cause sits just above the first `##[error]`.

    Falls back to the tail when nothing is marked, which is the right answer for a check that
    is not an Actions job and for a runner that died without writing a marker.
    """
    lines = [_LOG_TS.sub("", line) for line in log.splitlines()]
    marks = [i for i, line in enumerate(lines) if _ERROR_MARK in line]
    if not marks:
        return log[-max_chars:].strip()

    windows: list[list[int]] = []
    for i in marks:
        lo, hi = max(0, i - before), min(len(lines), i + after + 1)
        if windows and lo <= windows[-1][1]:
            windows[-1][1] = max(windows[-1][1], hi)
        else:
            windows.append([lo, hi])

    # Later failures are the ones worth keeping when the budget is tight: the first error in a
    # job often cascades, and the last is usually the step that actually stopped it.
    chunks: list[str] = []
    budget = max_chars
    for lo, hi in reversed(windows):
        chunk = "\n".join(lines[lo:hi]).strip()
        if not chunk:
            continue
        if len(chunk) > budget:
            chunk = chunk[-budget:]
        chunks.insert(0, chunk)
        budget -= len(chunk)
        if budget <= 0:
            break
    return "\n[...]\n".join(chunks)


async def failing_check_logs(
    repo: str, sha: str, limit: int = 2, max_chars: int = 4000
) -> str:
    """The tail of each failed check's log on `sha`, as plain text for an agent to read.

    The routing decision only ever saw a check's *name*. "gates=failure" cannot distinguish
    a defect in the change from a container registry that timed out, and both were sent to a
    fix agent as if they were the same thing — which is how two full runs were spent trying
    to repair a Docker Hub outage. Fetch the log once here and hand it to the next agent
    instead of making it go and find it, or guess.

    Best effort by design: context is a bonus, and never a reason to fail a run. A check that
    is not a GitHub Actions job has no log endpoint, so its reported output is used instead.
    """
    sections: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(
                f"{API}/repos/{repo}/commits/{sha}/check-runs", headers=_headers()
            )
            resp.raise_for_status()
            failed = [
                r
                for r in resp.json().get("check_runs", [])
                if r.get("status") == "completed" and r.get("conclusion") not in CHECK_OK
            ][:limit]

            for run in failed:
                body = ""
                try:
                    # A check run's id is the Actions job id. Anything else 404s here.
                    got = await client.get(
                        f"{API}/repos/{repo}/actions/jobs/{run.get('id')}/logs",
                        headers=_headers(),
                    )
                    if got.status_code == 200:
                        body = extract_failure(got.text, max_chars)
                except httpx.HTTPError:
                    body = ""
                if not body:
                    out = run.get("output") or {}
                    body = f"{out.get('summary') or ''}\n{out.get('text') or ''}".strip()
                    body = body[:max_chars]
                sections.append(
                    f"### {run.get('name', 'check')} = {run.get('conclusion')}"
                    f"  ({run.get('html_url', '')})\n{body or '(no log available)'}"
                )
    except Exception as exc:  # noqa: BLE001 - never fail a run over missing context
        return f"(could not fetch check logs: {exc!r})"
    return "\n\n".join(sections) or "(no failing check produced a log)"


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
