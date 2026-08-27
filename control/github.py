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
import base64
import re
import tempfile
from collections.abc import Callable

import httpx

from .config import settings

API = "https://api.github.com"
# Pushes are authorised by the git host, not the API host, and the difference matters:
# `can_push` below has to ask the endpoint that will actually enforce the answer.
GIT = "https://github.com"

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


def _api_error(resp) -> str:
    """GitHub's own explanation of a refusal, which `raise_for_status()` throws away.

    A status line on its own is not actionable. A 403 from the merge API is "this token may
    not write here", "you are being rate limited", or "a ruleset forbids it" — three different
    repairs, and only the body says which. `x-accepted-github-permissions` is appended because
    for a fine-grained token it names the exact grant that is missing, which is most of the
    answer in one header.
    """
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


async def list_repos(limit: int = 300) -> list[dict]:
    """Every repo this deployment's token can see, most recently pushed first.

    For the connect picker, and only for it. Connecting a repo has never needed this — the
    register takes an `owner/name` string and preflight asks GitHub the questions that matter
    — but "which repos are there" was a question only the person at the keyboard could
    answer, from memory, into a free-text box. A typo there is a 404 an hour later.

    `affiliation` rather than `type`: a factory is pointed at repos somebody owns, collaborates
    on, or reaches through an org, and the default (`owner,collaborator,organization_member`)
    is exactly that set. Sorted by `pushed` because the repo you want to connect is
    overwhelmingly one you touched recently.

    Paginated to `limit` and no further. A hard stop rather than "follow every page" because
    this feeds a dropdown: an account with four thousand repos should cost one second and a
    truncated list, not thirty seconds and a complete one.
    """
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
                    # What the *account* may do, which is not what the token may do — see
                    # `can_push`, which asks git rather than trusting this. Good enough to
                    # grey out a row in a picker; never good enough to skip preflight.
                    "can_push": bool((r.get("permissions") or {}).get("push")),
                }
                for r in body
                if r.get("full_name")
            )
            if len(body) < 100 or len(out) >= limit:
                break
    return out[:limit]


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


async def ref_sha(repo: str, ref: str) -> str | None:
    """The commit `ref` points at right now, or None when it cannot be resolved.

    This is the attribution key for everything the agent reads out of the repo rather than
    out of the prompt: `.mem/`, `.factory.md`, and `.claude/skills/` are all files at a
    commit, so the commit the run branched from is the one fact that says which version of
    each of them was in play. Recorded per run precisely so "did that change help?" can be
    answered by segmenting runs on it instead of by remembering when something merged.

    A failure here is not worth failing a dispatch over — an unattributed run is a run whose
    context version is unknown, which is exactly what every run before this column already
    was — so the caller gets None and the run proceeds.
    """
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                f"{API}/repos/{repo}/commits/{ref}",
                headers={**_headers(), "Accept": "application/vnd.github.sha"},
            )
            resp.raise_for_status()
        sha = resp.text.strip()
        return sha or None
    except Exception:  # noqa: BLE001 - attribution is observability, never a dispatch gate
        return None


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
                    if resp.status_code >= 400:
                        # Deliberately not raise_for_status(): its message is the status and
                        # nothing else. A merge that will not happen strands a green pull
                        # request for a human, and the only thing that human needs is the
                        # sentence GitHub already wrote.
                        raise httpx.HTTPStatusError(
                            f"merge refused: {resp.status_code} {_api_error(resp)}",
                            request=resp.request,
                            response=resp,
                        )
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


async def can_push(repo: str) -> tuple[bool, str]:
    """Whether this token may actually push to `repo` — asked of git, not of metadata.

    `GET /repos/{repo}` carries a `permissions` block, and reading `push` off it is the
    obvious thing to do and the wrong one: it describes what the *account* may do, so it says
    `push: true` for a repo you own however narrowly the token itself is scoped. A read-only
    token reads green there. Preflight said READY about this very repo while its token could
    not write a byte, and the first thing to notice was a pull request that could never merge.

    So ask git. Fetching `info/refs?service=git-receive-pack` is the handshake every push
    opens with: 200 for a credential that may write, 403 for one that may not. It sends no
    objects and updates no refs, so it cannot change anything — the question is asked in
    exactly the terms the answer will be enforced in.

    Returns (ok, detail). Never raises: an unreachable github.com is a finding, not a crash.
    """
    if not settings.github_token:
        return False, "no GitHub token is configured, so nothing can push"
    # Basic auth is how git speaks to GitHub over HTTPS. The username is not read when the
    # password is a token, so any constant does; this is the one GitHub's own docs use.
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


async def create_issue(
    repo: str, title: str, body: str, labels: list[str] | None = None
) -> dict:
    """Open an issue. Returns the created issue object.

    Labels are passed at creation rather than added afterwards, and that matters for exactly
    one of them: `agent:queued` is what the poller acts on, so an issue that exists for a
    moment without it and gains it later is an issue the poller can see half-formed. One call,
    one state.

    This is the only way the improvement loop reaches GitHub. The agent proposing a change
    writes a file; the control plane files it. That is deliberate — an allowlist enforced in
    a prompt is a request, and the same rule enforced here is a rule.
    """
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"{API}/repos/{repo}/issues",
            headers=_headers(),
            json={"title": title, "body": body, "labels": labels or []},
        )
        resp.raise_for_status()
    return resp.json()


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


# --------------------------------------------------------------------------- branch reconcile

# What GitHub says when it refuses a merge because the branch has diverged in a way its own
# merge could not resolve. Matched on the message because the status code does not separate
# this from the other things a 405 means.
_CONFLICT_PHRASES = ("merge conflict", "not mergeable")


def is_merge_conflict(exc: BaseException) -> bool:
    """Whether a failed merge failed *because the branch conflicts*, not for some other reason.

    A conflict is the one merge failure that can be repaired without a human, so it has to be
    told apart from a refused permission, a protected branch, or a required review — none of
    which reconciling would fix, and all of which arrive as the same 405.
    """
    return any(p in str(exc).lower() for p in _CONFLICT_PHRASES)


def _redact(text: str) -> str:
    """Strip the token out of anything on its way to a log."""
    token = settings.github_token
    return text.replace(token, "***") if token else text


async def _git(*args: str, cwd: str | None = None, timeout: float = 300) -> tuple[int, str]:
    """Run one git command, returning (exit code, combined output)."""
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, f"git {args[0]} timed out after {timeout}s"
    return proc.returncode or 0, _redact(out.decode("utf-8", "replace").strip())


def clone_url(repo: str) -> str:
    """Where to clone `repo` from, authenticated to push.

    A named function rather than an inline f-string because it is the seam the tests replace
    to point at a bare repository on disk — a merge driver either applies or it does not, and
    that is only worth asserting against real git.
    """
    return f"https://x-access-token:{settings.github_token}@github.com/{repo}.git"


async def merge_base_into_branch(
    repo: str, branch: str, base: str, write: Callable[[str], None]
) -> tuple[bool, str]:
    """Bring `branch` up to date with `base` using *the repo's own* merge configuration.

    This exists because GitHub's merge API is not git. It performs a three-way content merge
    with no working tree, and so it never reads the repository's `.gitattributes` — a repo that
    declares `merge=union` for its append-only logs gets that driver on every laptop and CI
    runner and does not get it here. The result is a pull request that merges cleanly with
    `git merge` and that GitHub refuses as conflicted, which is not a state anyone can act on
    by reading the pull request.

    That is not hypothetical: it is how foundation-e-learning#82 halted a queue of four issues
    with every check green, on one appended line of `.mem/index.jsonl` — the exact file whose
    `.gitattributes` entry exists to make that line mergeable.

    So do the merge where the drivers are: a throwaway clone, `git merge`, and push the result
    for GitHub to look at again. Returns (ok, detail); never raises.

    Deliberately narrow, because this writes to a branch:

    - It only ever merges *into* the pull request's own branch. `base` is read and never
      written, so the worst case is a factory branch nobody wanted.
    - No force, no rebase, no `-X ours`/`-X theirs`, no `--strategy`. If git says conflict,
      this aborts and reports — resolving a genuine disagreement is a human's call, and a
      strategy flag would silently pick a side of one.
    - "Already up to date" is a failure here, not a success: it means the branch did not
      diverge and the merge was refused for some other reason, which reconciling cannot fix.
    """
    if not settings.github_token:
        return False, "no GitHub token is configured, so nothing can push"
    with tempfile.TemporaryDirectory(prefix="factory-reconcile-") as tmp:
        url = clone_url(repo)
        code, out = await _git(
            "clone", "--filter=blob:none", "--no-single-branch", url, tmp, timeout=600
        )
        if code != 0:
            return False, f"could not clone {repo}: {out}"

        for key, value in (("user.name", "software-factory"),
                           ("user.email", "factory@users.noreply.github.com")):
            await _git("config", key, value, cwd=tmp)

        code, out = await _git("checkout", branch, cwd=tmp)
        if code != 0:
            return False, f"could not check out {branch}: {out}"

        code, out = await _git("merge", "--no-edit", f"origin/{base}", cwd=tmp)
        if code != 0:
            # `git merge` leaves the tree mid-merge on failure. Aborting is tidiness in a
            # directory about to be deleted, but it also makes `diff --name-only` below mean
            # what it says if this ever grows a second attempt.
            _, conflicted = await _git("diff", "--name-only", "--diff-filter=U", cwd=tmp)
            await _git("merge", "--abort", cwd=tmp)
            files = ", ".join(conflicted.split()) or "unknown files"
            return False, f"git could not merge {base} either — conflicts in {files}"
        if "Already up to date" in out:
            return False, f"{branch} is already up to date with {base}"

        code, out = await _git("push", "origin", branch, cwd=tmp)
        if code != 0:
            return False, f"could not push {branch}: {out}"

    write(f"[factory] merged {base} into {branch} with the repo's own merge drivers")
    return True, f"merged {base} into {branch}"
