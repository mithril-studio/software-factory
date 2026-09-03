"""What Sentry knows about the apps this factory built — pulled, never pushed.

Sentry owns the front half of bug tracking: its SDKs capture production exceptions with
context, fingerprint them into issues, count occurrences, and notice regressions. This
module is the factory's half of the bargain, and it is deliberately dumb — a REST client,
a per-repo provisioning step, and a sync loop that mirrors Sentry's issue list into the
`bugs` table. No judgement anywhere: deciding what to *do* about a bug is a later loop's
job, and per the passive-layer rule it will happen inside a VM, not here.

Two design points worth stating:

**Polling, not webhooks.** Sentry's issue list has queue semantics — it can be asked
"what is there now?" — so the same argument that keeps GitHub webhooks out of this plane
(docs/architecture.md §5) applies here: a poll every few minutes is one authenticated GET,
where a webhook is a public endpoint, an HMAC check, and a hole in the auth middleware.
Revisit only if a triage loop ever needs to hear about an error in seconds.

**REST, not the Sentry MCP.** The MCP is an agent-facing protocol; agents (Claude
sessions, later triage runs) use it conversationally. A control plane has no model to
speak it with, and the two calls it needs — create a project, list issues — are plain
HTTP, shaped exactly like `control/github.py`.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re

import httpx

from . import db, github, repos
from .config import settings

log = logging.getLogger("factory.sentry")

API = "https://sentry.io/api/0"

# The most issues one sync reads per repo, newest first. A cap because the table is a
# mirror for a UI and a future triage loop, not an archive — an app with more than this
# many distinct open issues has a problem no list length fixes. Sentry pages at 100.
SYNC_LIMIT = 100

_task: asyncio.Task | None = None


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.sentry_token}"}


def _team() -> str:
    """The team new projects land under. Sentry's default team shares the org's slug."""
    return settings.sentry_team or settings.sentry_org


def project_slug(repo: str) -> str:
    """The Sentry project slug for `repo`. Pure, so provisioning and lookups agree.

    Owner and name both, the way golden snapshots are named (`golden-<owner-repo>`),
    because two owners can share a repo name. Sentry slugs are lowercase alnum-and-dashes,
    at most 50 characters.
    """
    slug = re.sub(r"[^a-z0-9-]+", "-", repo.lower()).strip("-")
    return slug[:50].rstrip("-")


def bug_id(repo: str, sentry_issue_id: str) -> str:
    """A row id derived from the identity, so every sync lands the same issue on one row."""
    return hashlib.sha256(f"{repo}:{sentry_issue_id}".encode()).hexdigest()[:16]


def bug_row_from_issue(repo: str, payload: dict) -> dict:
    """One Sentry issue's JSON, reduced to the columns the `bugs` table holds.

    Pure and defensive: the payload is Sentry's to shape, so every field is optional here
    and the strings are truncated on the way in. `count` arrives as a string in Sentry's
    API — the int() is not decoration.
    """

    def text(key: str, cap: int = 300) -> str:
        return str(payload.get(key) or "")[:cap]

    def number(key: str) -> int:
        try:
            return int(payload.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    sentry_issue_id = str(payload.get("id") or "")
    return {
        "id": bug_id(repo, sentry_issue_id),
        "repo": repo,
        "sentry_issue_id": sentry_issue_id,
        "short_id": text("shortId", 60),
        "title": text("title"),
        "culprit": text("culprit"),
        "level": text("level", 20),
        "status": text("status", 30),
        "substatus": text("substatus", 30),
        "count": number("count"),
        "user_count": number("userCount"),
        "first_seen": text("firstSeen", 40),
        "last_seen": text("lastSeen", 40),
        "permalink": text("permalink", 400),
        "synced_at": db.utcnow(),
    }


# ------------------------------------------------------------------------- provisioning

# The two decisions the loop takes per repo, pure so they are testable without a network.
# Split in two because they fail independently: a created project whose wiring issue could
# not be filed is finished by the next tick, not re-created.


def should_provision(row: dict) -> bool:
    """Whether `row`'s repo still needs a Sentry project created (or found)."""
    return not (row.get("sentry_project") or "")


def should_file_wiring(row: dict) -> bool:
    """Whether the repo has a project and DSN but no wiring issue filed yet."""
    return bool(row.get("sentry_dsn")) and not row.get("sentry_wiring_issue")


WIRING_ISSUE_TITLE = "Wire Sentry error reporting into this app"

# The issue the factory files on a repo when its Sentry project is created — built by the
# factory itself, like any other queued issue. The DSN is written literally because it is a
# client-side identifier (it ships in browser bundles), not a secret. AC1 is `structure`
# rather than `test` because "the SDK is wired in" is a fact about the tree, and a grep is
# a command the reviewer can run on any stack without knowing the framework.
WIRING_ISSUE_TEMPLATE = """\
## Task
Add Sentry error reporting to this application, so production exceptions are captured and
the software factory can see what breaks after it ships.

Use the official Sentry SDK for this app's language and framework. Initialise it once, at
process or app startup, with this DSN (safe to commit — a DSN identifies the project, it
does not grant access to it):

    {dsn}

Set the SDK's `release` to the current git commit sha (at build time or startup), so an
error can be traced back to the exact code that raised it. Keep the wiring minimal: the
dependency, the init call, the release tag — nothing else.

## Acceptance criteria
```yaml
- id: AC1
  mode: structure
  statement: "The Sentry DSN is wired into the app's configuration or startup code."
  verify: "grep -rq '{dsn}' --exclude-dir=node_modules --exclude-dir=.git ."
- id: AC2
  mode: inspect
  statement: "The SDK initialises at startup and tags events with the git sha as release."
  verify: "the file where the SDK is initialised"
```

## Boundaries
- **Always:** keep the change minimal — SDK dependency, one init call, release tagging.
- **Stop and flag:** if the app has no clear startup point to initialise the SDK from.
- **Never:** remove or rework existing logging; structured logs stay as they are.

---
Filed by the factory: Sentry project `{project}` was provisioned for this repo, and this
issue is what connects the app to it.
"""


async def ensure_project(repo: str) -> tuple[str, str]:
    """Create `repo`'s Sentry project if it does not exist, and return (slug, dsn).

    Idempotent the cheap way: a create that 409s means the slug is taken — by this repo's
    own earlier provisioning, since the slug is derived — and either way the DSN comes from
    the keys listing, which is the part that matters.
    """
    slug = project_slug(repo)
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"{API}/teams/{settings.sentry_org}/{_team()}/projects/",
            headers=_headers(),
            json={"name": repo, "slug": slug},
        )
        if resp.status_code not in (201, 409):
            resp.raise_for_status()
        resp = await client.get(
            f"{API}/projects/{settings.sentry_org}/{slug}/keys/", headers=_headers()
        )
        resp.raise_for_status()
        keys = resp.json()
    if not keys:
        raise RuntimeError(f"sentry project {slug} exists but has no client keys")
    return slug, str(keys[0].get("dsn", {}).get("public") or "")


async def list_issues(slug: str, limit: int = SYNC_LIMIT) -> list[dict]:
    """The newest `limit` issues of one project, every status included.

    `query=""` on purpose: the endpoint defaults to `is:unresolved`, and a mirror that only
    ever saw unresolved issues would hold a row as `unresolved` forever after somebody
    resolved it in Sentry. Filtering is the UI's job, not the sync's.
    """
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            f"{API}/projects/{settings.sentry_org}/{slug}/issues/",
            headers=_headers(),
            params={"query": "", "limit": limit, "sort": "date"},
        )
        resp.raise_for_status()
        data = resp.json()
    return data if isinstance(data, list) else []


async def _provision(row: dict) -> None:
    """Give one repo its Sentry project, and file the wiring issue exactly once.

    Two recorded steps with the register updated after each, so a crash between them is
    resumed by the next tick rather than repeated: a project that exists is never created
    twice, and a wiring issue that was filed is never filed twice. The issue is labelled
    `agent:queued` at creation — by this plane, which is the only place allowed to — so
    the factory builds the wiring like any other issue.
    """
    repo = row["repo"]
    if should_provision(row):
        slug, dsn = await ensure_project(repo)
        await repos.record_sentry(repo, slug, dsn)
        row = repos.row(repo) or {**row, "sentry_project": slug, "sentry_dsn": dsn}
        log.info("%s: sentry project %s ready", repo, slug)
    if should_file_wiring(row):
        issue = await github.create_issue(
            repo,
            WIRING_ISSUE_TITLE,
            WIRING_ISSUE_TEMPLATE.format(
                dsn=row["sentry_dsn"], project=row["sentry_project"]
            ),
            labels=[github.LABEL_QUEUED],
        )
        await repos.record_sentry_wiring(repo, int(issue["number"]))
        log.info("%s: wiring issue #%s filed and queued", repo, issue["number"])


# --------------------------------------------------------------------------- the sync loop


async def sync_once() -> int:
    """One pass over the register: provision what is new, mirror what exists.

    Per-repo try/except, because the loop's job is the whole register: one repo's expired
    project or flaky listing must cost that repo a tick, not stop the others. Returns how
    many bug rows were written, for the log line.
    """
    written = 0
    for row in repos.rows():
        repo = row["repo"]
        try:
            await _provision(row)
            slug = (repos.row(repo) or row).get("sentry_project")
            if not slug:
                continue
            for payload in await list_issues(slug):
                await db.upsert_bug(bug_row_from_issue(repo, payload))
                written += 1
        except Exception as exc:  # noqa: BLE001 - a sync that cannot read skips, never crashes
            log.warning("%s: sentry sync skipped: %r", repo, exc)
    return written


async def _loop() -> None:
    log.info("syncing sentry every %ss", settings.sentry_sync_interval)
    while True:
        written = await sync_once()
        if written:
            log.info("synced %s bug row(s)", written)
        await asyncio.sleep(settings.sentry_sync_interval)


def start() -> None:
    """Launch the sync loop, unless the integration is off or cannot authenticate.

    Missing credentials are a warning and a refusal to start, not a crash: FACTORY_SENTRY=1
    with no token is a half-finished setup, and the rest of the factory works fine while
    somebody finishes it.
    """
    global _task
    if _task is not None or not settings.sentry_enabled or not settings.sentry_sync_interval:
        return
    if not (settings.sentry_org and settings.sentry_token):
        log.warning("FACTORY_SENTRY=1 but FACTORY_SENTRY_ORG or _TOKEN is unset; not syncing")
        return
    _task = asyncio.create_task(_loop())


async def stop() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except asyncio.CancelledError:
        pass
    _task = None
