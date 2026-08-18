"""Which agents this deployment can run, and how far any of them can be trusted.

This used to be a freshness sweep: hourly, it forked nothing but `exec`d into every golden
*machine* and asked how far behind its checkout was, whether the tree was dirty, whether a
dependency manifest had moved. A golden is a repo-agnostic snapshot now. It has no checkout,
so every one of those questions stopped having an answer at the same moment — and the one
that killed goldens was never on the list.

What kills a golden is credential expiry. The only real test of a credential is using it,
and the runs are already doing that, on the exact machine the question is about, for free.
So this grades by evidence: `verified_at` is when a run last finished on a snapshot having
produced usage, which is proof that its `claude` login and its `gh` token both still work.
Nothing here boots a VM, and nothing here repairs anything.

What is left costs one list call, which is why it runs every five minutes rather than every
hour: discovering an agent late is the cost that replaced discovering staleness late, and a
snapshot built by hand should be dispatchable within a poll or two of existing.
"""

from __future__ import annotations

import asyncio
import json
import logging

from . import agents, db, runner
from .config import settings

log = logging.getLogger("factory.goldens")

_task: asyncio.Task | None = None


def _manifest(raw: str | None) -> dict:
    """The manifest a run recorded, or `{}`. Never raises — it is stored input, not ours."""
    try:
        parsed = json.loads(raw or "")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def refresh() -> dict[str, dict]:
    """List the golden snapshots and record one row each. Returns the rows it wrote.

    Never raises: a boxd outage is a refresh that recorded nothing, not a control plane that
    fell over. It writes one row per *discovered* name and no rows for anything else, so a
    snapshot that has been deleted keeps its last row rather than being resurrected with an
    empty one — the fleet listing, not this table, is what `available()` answers from.
    """
    boxd = runner.client()
    try:
        # The refresh is the one caller that must not read a memoised fleet: it exists to
        # notice change, and a listing up to `CACHE_TTL` old is exactly what it is looking for.
        agents.forget()
        names = await agents.available(boxd)
    except Exception:  # noqa: BLE001 - a fleet nobody can list is nothing to record
        log.exception("could not list the golden snapshots")
        return {}
    finally:
        await boxd.close()

    evidence = await db.agent_evidence()
    checked_at = db.utcnow()
    rows: dict[str, dict] = {}
    for name in names:
        parsed = agents.parse_golden(name)
        seen = evidence.get(name, {})
        manifest = _manifest(seen.get("manifest"))
        rows[name] = {
            "agent": parsed[0] if parsed else None,
            "version": agents.version(name),
            # What the golden announced on the way into its last run. Read back from the
            # run rather than from the machine, because asking the machine means booting it.
            "events": manifest.get("events"),
            "transcript": manifest.get("transcript"),
            "manifest": seen.get("manifest"),
            "agent_version": manifest.get("version"),
            # `ok=0` with no error is a golden nothing has run on yet: unproven, not broken.
            "ok": seen.get("ok", 0),
            "error": seen.get("error"),
            "last_run": seen.get("last_run"),
            "verified_at": seen.get("verified_at"),
            "checked_at": checked_at,
        }
        await db.record_agent(name, **rows[name])

    unproven = [n for n, r in rows.items() if not r["verified_at"]]
    log.info(
        "%s golden snapshot(s): %s",
        len(rows),
        ", ".join(sorted(agents.discover(rows))) or "none",
    )
    if unproven:
        log.warning("no run has yet proved: %s", ", ".join(sorted(unproven)))
    return rows


async def _loop() -> None:
    log.info("refreshing agents every %ss", settings.agent_refresh_interval)
    while True:
        await refresh()
        await asyncio.sleep(settings.agent_refresh_interval)


def start() -> None:
    """Launch the refresh loop, unless it is switched off.

    No longer conditional on a repo being watched: which agents exist is a fact about the
    fleet, and the UI asks it of a deployment that watches nothing yet.
    """
    global _task
    if _task is not None or not settings.agent_refresh_interval:
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
