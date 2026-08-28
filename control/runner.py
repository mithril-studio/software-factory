"""Fork a VM, run an agent in it, collect the result, reap the VM.

This module is the whole factory. It contains no model call: it decides nothing about the
work itself, only about machine lifecycle. All intelligence is the agent inside the VM.
"""

from __future__ import annotations

import asyncio
import inspect
import hashlib
import json
import logging
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml
from boxd import AsyncBoxd, _mappers as _boxd_mappers

from telemetry import digest
from telemetry import store as telemetry_store
from telemetry.recorder import Recorder

from . import agents, db, github, memory
from .config import settings

# boxd reports a snapshot's timestamps in **milliseconds**, but the SDK's snapshot mapper reads
# them as seconds (`_epoch`, where the machine mapper alongside it correctly uses `_epoch_ms`),
# so `snapshots.list()` dies in `datetime.fromtimestamp` with "year 58577 is out of range".
# That takes golden discovery with it, and with it every dispatch: `agents.available` cannot
# name a single `golden-*`, so preflight's `fleet readable` check fails fatally and no issue
# can be picked up. Verified against SDK 0.2.2, 0.2.4 and 0.2.5 — all three fail
# identically, so there is no version to pin `boxd>=0.2.2` back to.
#
# Nothing in this repo reads a snapshot's timestamp, so the narrowest fix is to stop the
# mapper crashing rather than to reimplement the listing: an epoch beyond any date boxd could
# plausibly report is milliseconds, and is scaled back down. Delete this once the SDK maps
# snapshot timestamps with `_epoch_ms`; the patch is idempotent and self-identifying so that
# removing it is a one-line change and leaving it in is harmless.
_EPOCH_SECONDS_CEILING = 32_503_680_000  # 3000-01-01, far past anything boxd will report.


def _tolerate_millisecond_epochs() -> None:
    """Make the boxd SDK's epoch mappers accept milliseconds as well as seconds."""
    for name in ("_epoch", "_epoch_always"):
        original = getattr(_boxd_mappers, name, None)
        if original is None or getattr(original, "_factory_patched", False):
            continue

        def scaled(value, _original=original):
            if value and abs(value) > _EPOCH_SECONDS_CEILING:
                value = value // 1000
            return _original(value)

        scaled._factory_patched = True
        setattr(_boxd_mappers, name, scaled)


_tolerate_millisecond_epochs()


# `log` is the per-run RunLog throughout this file, so the module logger takes another name.
_log = logging.getLogger("factory.runner")

# Run ids of in-flight runs -> their asyncio task, so the UI can cancel them.
_tasks: dict[str, asyncio.Task] = {}
_semaphore: asyncio.Semaphore | None = None
_provision_semaphore: asyncio.Semaphore | None = None


def semaphore() -> asyncio.Semaphore:
    """How many build and review runs may execute at once.

    Created lazily so it binds to the running loop, not import-time.
    """
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(settings.max_concurrent)
    return _semaphore


def provision_semaphore() -> asyncio.Semaphore:
    """How many goldens may be warmed at once — a separate budget from the runs.

    Provisioning used to take `semaphore()`, which meant connecting three repos consumed the
    entire build concurrency until their installs finished. A warm-up is a speed-up that is
    allowed to be slow; a build queued behind three of them is a factory that has stopped.
    """
    global _provision_semaphore
    if _provision_semaphore is None:
        _provision_semaphore = asyncio.Semaphore(settings.max_provision)
    return _provision_semaphore


# Every VM a run creates is named from its run id with one of these prefixes: `run-` for a
# build, `rev-` for a review, `prov-` for warming a repo's golden, `plan-` for a goal-loop
# planning run, `learn-` for an improvement-loop learning run. Reconcile and the fleet view
# both key off them, so they live here rather than being spelled out at each site — a prefix
# known to one and not the other is a VM nobody reaps and nobody recognises.
RUN_PREFIX = "run-"
REVIEW_PREFIX = "rev-"
PROVISION_PREFIX = "prov-"
class BudgetExceeded(RuntimeError):
    """A run spent more than `FACTORY_MAX_RUN_COST` and was stopped.

    Its own type because of what must *not* happen next. Every other failure a build can
    suffer is worth another attempt — a crash, a timeout, a VM that vanished — and
    `_fail_run` retries up to `max_attempts`. This one is the opposite: the run was stopped
    precisely because it was consuming money without converging, and retrying it twice more
    is how one lost run costs three times the ceiling that was meant to bound it.
    """


PLAN_PREFIX = "plan-"
LEARN_PREFIX = "learn-"
VM_PREFIXES = (RUN_PREFIX, REVIEW_PREFIX, PROVISION_PREFIX, PLAN_PREFIX, LEARN_PREFIX)


def is_run_vm(name: str) -> bool:
    """True for a machine this factory created for a run: build, review or provisioning."""
    return name.startswith(VM_PREFIXES)


def vm_role(name: str) -> str:
    """What a machine in the fleet is: `run`, `review`, `provision`, `plan`, `learn`, or
    `other`.

    Read off the same prefixes `is_run_vm` sweeps on, so the fleet view and the reaper can
    never disagree about what belongs to the factory. `other` covers everything the factory
    did not create — a control plane, somebody's scratch machine — and, now that goldens are
    snapshots, it is where a golden still held as a machine shows up: a rollback artefact
    rather than a category.
    """
    if name.startswith(REVIEW_PREFIX):
        return "review"
    if name.startswith(PROVISION_PREFIX):
        return "provision"
    if name.startswith(PLAN_PREFIX):
        return "plan"
    if name.startswith(LEARN_PREFIX):
        return "learn"
    if name.startswith(RUN_PREFIX):
        return "run"
    return "other"


def client() -> AsyncBoxd:
    return AsyncBoxd(api_key=settings.boxd_api_key)


async def machine_id(boxd: AsyncBoxd, source: str) -> str:
    """Resolve a machine's id.

    The boxd SDK forks by machine **id**, not name — forking by name fails with
    `source VM not found` even though the machine lists fine. A golden is named, not
    numbered, so resolve it here. Accepts an id too, so either form works.
    """
    for m in await boxd.machines.list():
        if m.id == source or m.name == source:
            return m.id
    raise RuntimeError(f"golden VM {source!r} not found in the boxd fleet")


async def _is_snapshot(boxd: AsyncBoxd, source: str) -> bool:
    """Does `source` name a snapshot in this account?

    Asked of the fleet rather than of the name, because both kinds of source are named the
    same way: `golden-copy` is a snapshot on the new path and was a machine on the old one. A name alone cannot tell them apart, and guessing wrong picks the wrong API.
    """
    return any(s.id == source or s.name == source for s in await boxd.snapshots.list())


async def _provision(boxd: AsyncBoxd, source: str, vm_name: str):
    """Give a run its VM, from a snapshot when `source` is one and by forking when it is not.

    Goldens are moving from long-lived machines to snapshots: a running golden holds one of
    the account's 20 concurrent VM slots forever, a snapshot holds none. The fork path stays
    for as long as any deployment still points at a machine — it is the rollback.

    Both paths set the same two timers, and both are load-bearing. Without
    `auto_suspend_timeout=0` the default idle suspend freezes a VM in the middle of a long,
    silent build. Without `auto_destroy_timeout` a control plane that dies leaks the slot for
    good. The snapshot path passes nothing else: `create(from_snapshot=...)` rejects `env`,
    `image`, `cmd`, `restart_policy`, `shared` and `networks` with a ValueError, since
    restoring a capture replays a machine rather than configuring a new one. The per-run
    environment arrives later, through `stream_exec(env=...)`, exactly as it does on the fork
    path.
    """
    if await _is_snapshot(boxd, source):
        return await boxd.machines.create(
            name=vm_name,
            from_snapshot=source,
            auto_suspend_timeout=0,
            auto_destroy_timeout=settings.auto_destroy,
        )
    return await boxd.machines.fork(
        await machine_id(boxd, source),
        vm_name,
        auto_suspend_timeout=0,
        auto_destroy_timeout=settings.auto_destroy,
    )


async def reap(boxd: AsyncBoxd, machine, log: RunLog, *, keep: bool = False) -> None:
    """Destroy a run's VM. Idempotent, and it never raises.

    Every path that creates a machine ends here, including the ones that got there by
    crashing. A machine that outlives its run holds one of the account's 20 slots until the
    `auto_destroy` timer fires two hours later, and three of those is a factory that cannot
    dispatch — so a failure to reap is logged and handed to the reconciler rather than raised
    into a `finally` that is already handling something else.
    """
    if machine is None:
        return
    if keep:
        log.write(f"[factory] keeping {machine.name} for inspection (FACTORY_KEEP_FAILED=1)")
        return
    try:
        await boxd.machines.delete(machine.id)
        log.write(f"[factory] destroyed {machine.name}")
    except Exception as exc:  # noqa: BLE001 - already gone is fine, and a leak is the reaper's
        log.write(f"[factory] could not destroy {machine.name}: {exc!r}; reconcile will sweep it")


async def headroom(boxd: AsyncBoxd, log: RunLog) -> None:
    """Refuse to provision when the fleet is at its cap, after trying to make room.

    Nothing counted machines against the quota before this: concurrency was bounded by
    `FACTORY_MAX_CONCURRENT`, which says nothing about the goldens, the control plane, or a
    machine somebody left running by hand. Past the cap boxd refuses the create, and the run
    that finds out is the one that dies.

    So ask first, and sweep before giving up — an orphaned run VM is exactly the thing
    `reconcile` exists to reclaim, and a factory that reaps and then proceeds is better than
    one that stops at a limit it could have cleared itself.
    """
    if settings.max_machines <= 0:
        return
    count = len(await boxd.machines.list())
    if count < settings.max_machines:
        return
    log.write(f"[factory] fleet at {count}/{settings.max_machines}; sweeping for orphans")
    swept = await reconcile()
    count = len(await boxd.machines.list())
    if count >= settings.max_machines:
        raise RuntimeError(
            f"the boxd fleet is at {count}/{settings.max_machines} machines and sweeping "
            f"reclaimed {len(swept.get('destroyed') or [])}; nothing can be provisioned until "
            "something is destroyed"
        )
    log.write(f"[factory] reclaimed to {count}/{settings.max_machines}")


async def source_for(boxd: AsyncBoxd, repo: str, log: RunLog | None = None) -> str:
    """Which golden this run boots from: this repo's own, or the base image behind it.

    Public because a run is not the only thing that needs the answer — the projects page and
    preflight both report on the snapshot a repo's runs would actually boot, and a second
    derivation of that would be a second thing to keep in step. Without a `log` the fallback
    is noted on the module logger instead of in a run's own log.

    One fallback, and it is the ordinary case rather than an error path: a repo nobody has
    provisioned a golden for yet boots `golden-copy` and installs for itself. That is what
    lets a repo be connected and dispatched in the same minute, with provisioning catching up
    afterwards.

    When the snapshot fleet answers to nothing at all, the base name is handed to `_provision`
    anyway. On the rollback path `golden-copy` is still a *machine*, and forking it is exactly
    what should happen.
    """
    note = log.write if log is not None else _log.info
    names = await agents.available(boxd)
    source = agents.resolve_snapshot(repo, names)
    if not source:
        source = agents.BASE_SNAPSHOT
        note(f"[factory] no golden snapshot in the fleet; trying machine {source}")
    elif source == agents.BASE_SNAPSHOT:
        note(f"[factory] no warm golden for {repo}; booting {source} and installing for itself")
    return source


# --------------------------------------------------------------------------- memory receipt

# The line an agent prints right after priming from `.mem/`, so retrieval is observable from
# the run log alone — no scraping a transcript, no coupling this to any one runtime's event
# shapes. Kept to counts, ids, and domain names; a memory *body* must never be echoed back
# into a log.
MEMORY_RECEIPT_MARKER = "FACTORY_MEMORY"

# What a repo with no `.mem/` reports — an explicit empty receipt rather than silence, so
# "nothing to prime" and "the agent never got to step 1" don't look the same downstream.
EMPTY_MEMORY_RECEIPT = {"indexed": 0, "opened": [], "domains": []}


def parse_memory_receipt(text: str) -> dict | None:
    """Pull the `FACTORY_MEMORY` priming receipt out of a run's output.

    Pure and read-only: scans `text` line by line for one that starts with the marker,
    parses the JSON after it, and returns the first line that validates as
    `{"indexed": int, "opened": [str, ...], "domains": [str, ...]}`. Everything else — a
    line that only looks like the marker, JSON that doesn't parse, a shape that's missing or
    mistypes a field, ordinary agent chatter with no receipt at all — yields `None`. Never
    raises: a malformed receipt must cost nothing, not crash the caller that's asking whether
    one was printed.
    """
    if not text:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith(MEMORY_RECEIPT_MARKER):
            continue
        payload = line[len(MEMORY_RECEIPT_MARKER):]
        if not payload or not payload[0].isspace():
            continue
        try:
            obj = json.loads(payload.strip())
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        indexed = obj.get("indexed")
        opened = obj.get("opened")
        domains = obj.get("domains")
        if not isinstance(indexed, int) or isinstance(indexed, bool) or indexed < 0:
            continue
        if not isinstance(opened, list) or not all(isinstance(x, str) for x in opened):
            continue
        if not isinstance(domains, list) or not all(isinstance(x, str) for x in domains):
            continue
        return {"indexed": indexed, "opened": opened, "domains": domains}
    return None


def _receipt_candidates(event: dict) -> list[str]:
    """Text blocks inside one stream event that might carry a `FACTORY_MEMORY` receipt.

    The agent can satisfy the prompt's "print one line" instruction either as its own
    turn text or by running a shell command that echoes it — the first shows up in an
    assistant `text` block, the second in a `tool_result`'s content. Both come back with
    real newlines once the JSON envelope is decoded, which a raw scan of the wire line
    (still carrying `\\n` as two characters) cannot find.
    """
    texts: list[str] = []
    message = event.get("message")
    if not isinstance(message, dict):
        return texts
    for block in message.get("content", []) or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            texts.append(str(block.get("text") or ""))
        elif block.get("type") == "tool_result":
            content = block.get("content")
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        texts.append(str(item.get("text") or ""))
    return texts


def _better_receipt(current: dict | None, texts: Iterable[str]) -> dict | None:
    """The first receipt in `texts` worth replacing `current` with, or `None` for none.

    A receipt that opened nothing is accepted but held provisionally, and here is why: the
    build prompt spells the empty receipt out literally, as valid JSON, so that an agent with
    no `.mem/` can copy it. Anything that echoes the prompt back through the stream — a
    `cat` of the prompt file, a subagent quoting its instructions, the transcript of a
    retry — therefore carries a parseable empty receipt. Stopping at the first match would
    let that echo claim the run, and the real receipt printed seconds later would be
    discarded as a duplicate. So a run keeps listening until something says it opened a
    record, and telemetry corrects itself: the run-level row REPLACEs, the per-record rows
    are keyed and IGNORE, so an upgrade costs one extra write and no wrong row.

    A receipt identical to the one already held is not a correction and writes nothing.
    """
    if current is not None and current.get("opened"):
        return None
    for text in texts:
        candidate = parse_memory_receipt(text)
        if candidate is not None and candidate != current:
            return candidate
    return None


async def _persist_memory_receipt(run_id: str, receipt: dict, log: RunLog) -> None:
    """Turn one parsed `FACTORY_MEMORY` receipt into telemetry rows, one per opened record.

    Best-effort by design (AC4): the receipt is an observability signal riding along on an
    otherwise-successful run, so a telemetry write that fails here — a locked database, a
    schema that has not migrated — is logged and swallowed rather than allowed to fail or
    stop the agent stream that produced it.

    A receipt with nothing `opened` still writes its run-level row. That is the whole reason
    the empty receipt is explicit rather than silence (`EMPTY_MEMORY_RECEIPT`): "the agent
    primed and found nothing worth opening" and "the agent never reached step 1" are different
    facts about a run, and dropping the row here would have made them identical downstream —
    the exact confusion the contract was written to prevent. Only the per-record rows depend
    on `opened`; there is no record to attribute those to.
    """
    opened = receipt.get("opened") or []
    try:
        ts = db.utcnow()
        # Two writes, at the two levels the receipt actually speaks at: one row per record it
        # opened, and one row for the run saying how big the index was and which domains it
        # drew from. The domains are not split across the records — the receipt names them as
        # a set and says nothing about which record came from which — so nothing here picks
        # one, and nothing can pick wrong.
        if opened:
            await telemetry_store.write_memory_reads(
                run_id, [(memory_id, ts) for memory_id in opened]
            )
        await telemetry_store.write_memory_receipt(
            run_id, receipt.get("indexed") or 0, receipt.get("domains") or [], ts
        )
    except Exception as exc:  # noqa: BLE001 - telemetry must never fail an otherwise good run
        log.write(f"[factory] memory receipt not recorded: {exc!r}")


# --------------------------------------------------------------------------- memory candidates

# Where the agent writes the learnings it wants to propose, and the variable that tells it.
# A file rather than a marker in the stream, because a candidate is a paragraph with evidence
# attached, and a run that could file one by printing would turn every stray line of narration
# into a queue entry. The path is per-run and inside the VM, which dies with the run.
MEMORY_CANDIDATE_ENV = "FACTORY_MEMORY_CANDIDATES"
MEMORY_CANDIDATE_PATH = "/tmp/factory-memory-candidates.jsonl"

# Bounds, so a run cannot turn its transcript into a queue. Each is enforced on the way in and
# every rejection is logged with its reason — a silent truncation reads as "nothing more was
# proposed", which is the one thing a triage queue must never imply.
MEMORY_CANDIDATE_MAX_BYTES = 64 * 1024
MEMORY_CANDIDATE_MAX_RECORDS = 20
MEMORY_CANDIDATE_MAX_TITLE = 200
MEMORY_CANDIDATE_MAX_BODY = 4000

# A candidate is a proposed memory record, so it answers to the memory skill's vocabulary
# rather than a second one invented here.
MEMORY_CANDIDATE_TYPES = memory.VALID_TYPES
MEMORY_CANDIDATE_CONFIDENCE = memory.VALID_CONFIDENCE

_CANDIDATE_REQUIRED = ("domain", "type", "title", "body", "evidence")
_DOMAIN_NAME = re.compile(r"[a-z0-9_][a-z0-9_-]*\Z")


def _candidate_paths(evidence: dict) -> tuple[list[str], str | None]:
    """The repository-relative paths an evidence object names, or why it has none usable.

    Repository-relative is the whole check. `/etc/passwd` and `../../secrets` are not evidence
    about this repository, and a candidate carrying one is either confused or reaching — either
    way it is not something to store and later show a reviewer as this repo's own learning.
    """
    if not isinstance(evidence, dict):
        return [], "evidence is not an object"
    paths = []
    for key in ("files", "dirs"):
        value = evidence.get(key)
        if value is None:
            continue
        if not isinstance(value, list):
            return [], f"evidence.{key} is not a list"
        for item in value:
            if not isinstance(item, str) or not item.strip():
                return [], f"evidence.{key} holds something that is not a path"
            path = item.strip()
            if path.startswith("/") or path.startswith("~"):
                return [], f"evidence path {path!r} is absolute, not repository-relative"
            if any(part == ".." for part in PurePosixPath(path).parts):
                return [], f"evidence path {path!r} climbs out of the repository"
            paths.append(path)
    if not paths:
        return [], "evidence names no file or directory in this repository"
    return paths, None


def _candidate_id(run_id: str, record: dict) -> str:
    """A stable id for one proposal, so collecting twice is one candidate and not two.

    Derived from the run and the content rather than handed out by the agent: an id the agent
    chose is an id the agent can reuse for a different learning, and `INSERT OR IGNORE` would
    then silently keep the first and drop the second.
    """
    payload = json.dumps(
        {k: record.get(k) for k in _CANDIDATE_REQUIRED}, sort_keys=True, ensure_ascii=False
    )
    digest = hashlib.sha256(f"{run_id}\x00{payload}".encode()).hexdigest()[:16]
    return f"cand_{digest}"


def parse_memory_candidates(text: str, run_id: str) -> tuple[list[dict], list[str]]:
    """Split a candidate artifact into what may be queued and what was refused, and why.

    Pure and total: it reads text an agent wrote inside a VM and returns
    `(accepted, rejections)`. Nothing here raises and nothing here decides a candidate is
    *true* — admission is about shape, scope and bounds, and the reviewer decides the rest.
    """
    rejections: list[str] = []
    if not text:
        return [], rejections
    raw = text.encode("utf-8", errors="replace")
    if len(raw) > MEMORY_CANDIDATE_MAX_BYTES:
        return [], [
            f"candidate artifact is {len(raw)} bytes, over the "
            f"{MEMORY_CANDIDATE_MAX_BYTES}-byte limit; nothing was queued"
        ]

    accepted: list[dict] = []
    seen: set[str] = set()
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        if len(accepted) >= MEMORY_CANDIDATE_MAX_RECORDS:
            rejections.append(
                f"line {lineno}: over the {MEMORY_CANDIDATE_MAX_RECORDS}-candidate limit "
                f"for one run"
            )
            continue
        record, why = _read_candidate(line, lineno)
        if why is not None:
            rejections.append(why)
            continue
        record["id"] = _candidate_id(run_id, record)
        if record["id"] in seen:
            rejections.append(f"line {lineno}: the same candidate was already proposed")
            continue
        seen.add(record["id"])
        accepted.append(record)
    return accepted, rejections


def _read_candidate(line: str, lineno: int) -> tuple[dict, None] | tuple[None, str]:
    """One line of the artifact, as a queueable record or as the reason it is not one."""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as exc:
        return None, f"line {lineno}: not JSON ({exc.msg})"
    if not isinstance(obj, dict):
        return None, f"line {lineno}: not a JSON object"
    missing = [f for f in _CANDIDATE_REQUIRED if not obj.get(f)]
    if missing:
        return None, f"line {lineno}: missing {', '.join(missing)}"

    domain = str(obj["domain"]).strip()
    if not _DOMAIN_NAME.match(domain):
        return None, f"line {lineno}: {domain!r} is not a domain slug"
    if obj["type"] not in MEMORY_CANDIDATE_TYPES:
        return None, f"line {lineno}: unknown type {obj['type']!r}"
    # The memory skill's own rule (§3.1), applied here rather than after the fact: a `failure`
    # without its fix is the half of the learning that costs time and none of the half that
    # saves it.
    if obj["type"] == "failure" and not str(obj.get("resolution") or "").strip():
        return None, f"line {lineno}: type 'failure' with no resolution"
    confidence = str(obj.get("confidence") or "medium")
    if confidence not in MEMORY_CANDIDATE_CONFIDENCE:
        return None, f"line {lineno}: unknown confidence {confidence!r}"

    title = str(obj["title"]).strip()
    body = str(obj["body"]).strip()
    if len(title) > MEMORY_CANDIDATE_MAX_TITLE:
        return None, f"line {lineno}: title is {len(title)} characters, over {MEMORY_CANDIDATE_MAX_TITLE}"
    if len(body) > MEMORY_CANDIDATE_MAX_BODY:
        return None, f"line {lineno}: body is {len(body)} characters, over {MEMORY_CANDIDATE_MAX_BODY}"

    paths, why = _candidate_paths(obj["evidence"])
    if why is not None:
        return None, f"line {lineno}: {why}"

    evidence = {"files": [], "dirs": []}
    for key in ("files", "dirs"):
        value = obj["evidence"].get(key)
        if isinstance(value, list):
            evidence[key] = [v.strip() for v in value if isinstance(v, str) and v.strip()]
    if obj["type"] == "failure":
        evidence["resolution"] = str(obj["resolution"]).strip()
    return {
        "domain": domain,
        "type": obj["type"],
        "title": title,
        "body": body,
        "evidence": json.dumps(evidence, ensure_ascii=False),
        "confidence": confidence,
    }, None


async def _collect_memory_candidates(
    boxd: AsyncBoxd, machine_id: str, run_id: str, repo: str, log: RunLog
) -> int:
    """Read the run's candidate artifact out of the VM and queue what passes.

    Called before the VM is destroyed, on every path that had one — a run that failed may
    have learned the most useful thing in the batch, and a run that is about to be reaped is
    the last moment the file exists.

    Best effort, like the transcript salvage beside it: nothing about proposing a learning is
    worth failing a run that has already done its work.
    """
    script = (
        f'[ -f "{MEMORY_CANDIDATE_PATH}" ] && '
        f'[ "$(wc -c < "{MEMORY_CANDIDATE_PATH}")" -le {MEMORY_CANDIDATE_MAX_BYTES * 2} ] && '
        f'cat "{MEMORY_CANDIDATE_PATH}" || true'
    )
    try:
        result = await boxd.machines.exec(machine_id, script, timeout=60)
        text = result.stdout or ""
        if not text.strip():
            return 0
        accepted, rejections = parse_memory_candidates(text, run_id)
        for why in rejections:
            log.write(f"[factory] memory candidate rejected: {why}")
        for record in accepted:
            await db.create_candidate(run_id=run_id, repo=repo, **record)
        if accepted:
            log.write(
                f"[factory] {len(accepted)} memory candidate(s) queued for review"
                + (f", {len(rejections)} rejected" if rejections else "")
            )
        return len(accepted)
    except Exception as exc:  # noqa: BLE001 - a proposal is never worth a run
        log.write(f"[factory] memory candidates not collected: {exc!r}")
        return 0


# --------------------------------------------------------------------------- prompt


PROMPT_TEMPLATE = """You are working autonomously in an isolated VM on a checked-out git \
repository. Resolve the GitHub issue below, then open a pull request.

Repository: {repo}
Issue #{number}: {title}

--- issue body ---
{body}
--- end issue body ---

How to work:
1. Load the `memory` skill first and prime yourself from `.mem/` if it exists. What past
   runs learned about this repo is the most valuable context you have. Immediately after
   priming — even when `.mem/` does not exist — print one machine-readable receipt line, so
   what you actually loaded is observable without anyone scraping this transcript:
   `{memory_receipt_marker} {{"indexed": <n>, "opened": ["mem_...", ...], "domains": ["..."]}}`
   `indexed` is how many records `.mem/index.jsonl` listed, `opened` is exactly the record
   ids whose full body you read, and `domains` is the domains they came from — never the
   record bodies themselves. When `.mem/` does not exist, print the explicit empty receipt
   instead of staying silent: `{memory_receipt_marker} {empty_memory_receipt}`.
2. Install this project's dependencies, before you touch any code. Run the setup command
   named under "This project" below, in the foreground, with an explicit timeout — e.g.
   `timeout 900 <the setup command>`. Run it **once**: if it worked, a second install spends
   minutes of wall clock arriving where you already are, and if it failed, read the error and
   fix that rather than run the same command again.
3. Make the change. Stay in scope: resolve this issue, nothing more.
   If the issue carries an `## Acceptance criteria` block, that is the contract. Every
   criterion must be true when you are done, and a reviewing agent will afterwards run each
   one rather than take your word for it. For `mode: test` criteria, write the test at the
   path given in `verify`, and make sure it **fails before your change and passes after** —
   a test that passes either way proves nothing and will be rejected.
4. Commit and push as you go — after each meaningful step, not once at the end:
   git add -A && git commit -m "<message>" && git push -u origin {branch}
   The branch is what survives; this VM is destroyed when you exit. If this run dies
   half-way, whatever you pushed is what the next attempt continues from, so small
   commits that build on each other are worth far more than one perfect commit you
   never got to make.
5. Verify with the repo's own fast checks — the ones named under "This project" below,
   and only those. Do not add a test framework or test runner that isn't already in the repo.
6. Record anything durable you learned into `.mem/`, following the memory skill.
   Separately, if you learned something reusable that you are **not** confident enough to
   write into `.mem/` yourself, propose it: append one JSON object per line to the file named
   by `${memory_candidate_env}` (`{memory_candidate_path}`), before you finish. These are
   **candidates, not memory** — nothing you write there enters `.mem/` or reaches another run
   until a human accepts it, so proposing costs a later reviewer thirty seconds and nothing
   else. One line per learning:
   `{{"domain": "<slug>", "type": "convention|failure|pattern|decision|reference", "title": "<one line>", "body": "<a few sentences, including why>", "evidence": {{"files": ["path/in/this/repo"], "dirs": []}}, "confidence": "high|medium|low"}}`
   A `failure` also needs `"resolution"`: what actually fixed it.
   Propose one only if it is all six of: **novel** (not already in `.mem/`), **specific**
   (a fact, not a sentiment), **reusable** (a future run on this repo would want it),
   **scoped** (evidence paths inside this repository, never absolute), **evidence-backed**
   (you saw it in the code or in a command's output), and **not already obvious** from the
   README, CLAUDE.md, `.factory.md` or the code itself. At most {memory_candidate_max}, and
   usually zero — a run that discovered nothing new writes nothing. Never put a secret, a
   token, or a transcript excerpt in one.
7. Push the final commit and open a pull request with `gh pr create --fill --base {base}`,
   referencing the issue in the body so it links (e.g. "Closes #{number}").

This project — the package manager's download caches on this machine are warm, so installing
is fast, but nothing is installed for this repo yet:

{project_notes}

True of every run here, whatever the project notes say:
- Run long commands in the foreground with an explicit timeout, e.g.
  `timeout 600 <the build command>`. Do not put work in the background to wait for it later:
  background tasks and scheduled wake-ups do not deliver notifications in this
  environment, so a run that waits for one waits forever and dies having produced nothing.

If you cannot resolve the issue, still push what you have and open a draft PR explaining
what blocked you. A run that ends with no PR gives the human nothing to look at.
"""


# --------------------------------------------------------------------------- criteria

# The acceptance-criteria block an issue carries, per the factory-compose issue template
# (mithril-studio/agent-skills, factory-skills/factory-compose): a
# fenced yaml list under an "## Acceptance criteria" heading.
_CRITERIA_BLOCK = re.compile(
    r"^##\s*Acceptance criteria\s*?\n+```ya?ml\n(.*?)^```", re.S | re.M | re.I
)

# Modes whose verdict may block a merge. `inspect` needs human judgement, so it is reported
# and never blocks — an agent's opinion is not a gate.
BLOCKING_MODES = ("test", "probe", "structure")


# Why a review run ended where it did, written into its `error` column.
#
# A review's `status` answers a different question from its outcome: a reviewer that runs
# cleanly and refuses the change is a `succeeded` run, and so is one that approves a change
# CI then rejects. The outcome lives in `error`, which had three possible authors and no way
# to tell them apart — so the interface rendered an *approved* review whose pull request went
# red on CI as "changes requested", the opposite of what the reviewer said. That is what
# happened to foundation-e-learning#77, and reading it off the runs list was misleading in the
# one direction that matters: it blamed the change for a coverage floor the agent had never
# been told to run.
#
# Prefixes rather than a fourth column because the sentence after them is the thing a human
# actually reads, and it is already stored. `web/src/lib/api.ts:runOutcome` is the other side
# of this contract; `review_outcome_test.py` holds the two together.
REVIEW_REFUSED = "changes requested: "
REVIEW_CI_RED = "ci red: "
REVIEW_UNMERGED = "not merged: "


def parse_criteria(body: str) -> list[dict]:
    """Extract an issue's acceptance criteria. Returns [] when there are none to run.

    Deliberately done here rather than by the reviewing agent: what the criteria *are* is not
    a judgement call, and an agent that reads its own contract can also misread it. A malformed
    block yields [] — which skips review rather than inventing criteria, so the failure mode is
    "unreviewed like before", never "reviewed against something nobody wrote".
    """
    match = _CRITERIA_BLOCK.search(body or "")
    if not match:
        return []
    try:
        parsed = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return []
    if not isinstance(parsed, list):
        return []
    out = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        if not {"id", "mode", "statement"} <= set(item):
            continue
        if item["mode"] not in (*BLOCKING_MODES, "inspect"):
            continue
        out.append(item)
    return out


def _run_link(run_id: str) -> str | None:
    return f"{settings.base_url}/runs/{run_id}" if settings.base_url else None


async def _mirror_issue(
    repo: str,
    number: int,
    add: str | None,
    remove: list[str],
    log: RunLog,
    comment: str | None = None,
) -> None:
    """Reflect run state onto the issue as a label, optionally leaving a comment.

    Labels mirror the runs table for humans reading GitHub; they are never read back as
    truth. So this is best-effort by design — a GitHub hiccup is logged and swallowed, never
    allowed to fail an otherwise good run.
    """
    try:
        for label in remove:
            await github.remove_label(repo, number, label)
        if add:
            await github.add_labels(repo, number, [add])
        if comment:
            await github.add_comment(repo, number, comment)
    except Exception as exc:  # noqa: BLE001
        log.write(f"[factory] issue update skipped: {exc!r}")


# Two headers over one body, because the two situations are not the same and an agent told
# the wrong one debugs the wrong thing. A retry follows a run that crashed. A fix run follows
# a run that finished, opened a pull request, and had it sent back — telling that agent its
# "earlier attempts failed" points it at a failure that did not happen.
RETRY_HEADER = """This is attempt {attempt} of {max_attempts}. Earlier attempts on this issue \
failed ({prior_error}). Read the log below, work out the root cause, and fix *that* — do not
blindly repeat what failed."""

FIX_HEADER = """This is fix cycle {cycle} of {max_cycles}. The earlier run on this issue did \
not fail: it opened a pull request, and that pull request was sent back ({prior_error}). Read
the detail below and fix exactly what it names — the rest of the change was accepted."""

RETRY_TEMPLATE = """

--- {label} context ---
{header}

You are already checked out on {branch}, including the commits already pushed to it — read
them first (`git log --oneline origin/{base}..HEAD` and `git diff origin/{base}`) and continue
from there. Do not start over, and do not force-push over that work.

{detail_label}:
{prior_log}
--- end {label} context ---
"""


# The file a watched repo carries to describe itself: how it is set up, how it is verified,
# what must not be touched. It lives in the repo rather than here because it
# describes that repo — one project's `npm run test:integration` is another project's
# nonsense, and a control plane that asserts one project's setup as universal sends every
# other project's agent to verify something that does not exist.
PROFILE_PATH = ".factory.md"

# What a repo without a profile gets. Deliberately says nothing specific: a wrong fact costs
# more than a missing one, because the agent acts on it before it can find out.
DEFAULT_PROJECT_NOTES = """- Nothing is installed for this repo yet. Work out the package manager from the lock file
  at the root and run its frozen-lockfile install once — `package-lock.json` -> `npm ci`,
  `pnpm-lock.yaml` -> `pnpm install --frozen-lockfile`, `yarn.lock` -> `yarn install
  --immutable`, `uv.lock` -> `uv sync --frozen`, `poetry.lock` -> `poetry install --sync`,
  `Cargo.lock` -> `cargo fetch`, `go.sum` -> `go mod download`. Frozen, so the install is the
  one the lock file describes and your run does not silently upgrade the project. Do not
  delete or regenerate a lock file, and do not add a package the issue does not need.
- This repo carries no `.factory.md`, so nothing more specific about it is known here. Read
  its own rules files (`CLAUDE.md`, `AGENTS.md`, `CONTEXT.md`, the README) and `.mem/` to
  find out how it is built and verified, and run those checks.
- Do not run an end-to-end or browser suite, and do not install browsers — CI covers that on
  your pull request."""


async def project_notes(repo: str, ref: str) -> str:
    """The repo's own `.factory.md` at `ref`, or the generic default when it has none.

    Best-effort by design: a GitHub hiccup here must not fail a run that would otherwise
    work, and the default is a safe thing to say about any repo.
    """
    try:
        text = await github.file(repo, PROFILE_PATH, ref)
    except Exception:  # noqa: BLE001 - a profile is an improvement, never a precondition
        _log.exception("could not read %s from %s", PROFILE_PATH, repo)
        return DEFAULT_PROJECT_NOTES
    return text.strip() if text and text.strip() else DEFAULT_PROJECT_NOTES


def build_prompt(
    repo: str,
    issue: dict,
    branch: str,
    base: str,
    notes: str = DEFAULT_PROJECT_NOTES,
    attempt: int = 1,
    prior_error: str | None = None,
    prior_log: str | None = None,
    review_cycle: int = 1,
) -> str:
    prompt = PROMPT_TEMPLATE.format(
        repo=repo,
        number=issue["number"],
        title=issue["title"],
        body=issue["body"] or "(no description given)",
        branch=branch,
        base=base,
        project_notes=notes,
        memory_receipt_marker=MEMORY_RECEIPT_MARKER,
        empty_memory_receipt=json.dumps(EMPTY_MEMORY_RECEIPT),
        memory_candidate_env=MEMORY_CANDIDATE_ENV,
        memory_candidate_path=MEMORY_CANDIDATE_PATH,
        memory_candidate_max=MEMORY_CANDIDATE_MAX_RECORDS,
    )
    # Keyed on the cycle and the attempt, not on the attempt alone. A fix run is attempt 1 of
    # cycle 2, so `attempt > 1` was false for it — and the review findings or CI log the whole
    # run exists to act on were assembled, passed in, and then silently dropped from the
    # prompt. The agent was dispatched onto a branch it had never seen with no idea why.
    fix = review_cycle > 1
    if fix or attempt > 1:
        header = (
            FIX_HEADER.format(
                cycle=review_cycle,
                max_cycles=settings.max_review_cycles,
                prior_error=prior_error or "reason not captured",
            )
            if fix
            else RETRY_HEADER.format(
                attempt=attempt,
                max_attempts=settings.max_attempts,
                prior_error=prior_error or "reason not captured",
            )
        )
        prompt += RETRY_TEMPLATE.format(
            label="fix" if fix else "retry",
            header=header,
            detail_label="what came back" if fix else "previous attempt log (tail)",
            prior_log=prior_log or "(previous log unavailable)",
            branch=branch,
            base=base,
        )
    return prompt


def _log_tail(run_id: str, max_chars: int = 4000) -> str:
    """The tail of a run's log — the failure context handed to the next attempt."""
    path = settings.log_dir / f"{run_id}.log"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(previous log unavailable)"
    return text[-max_chars:]


# The script the VM runs. Values arrive as environment variables so nothing needs shell
# quoting here — the prompt in particular can contain anything at all.
# Runs under /bin/sh (dash) via boxd exec, NOT bash — keep it POSIX. No `set -o pipefail`
# (a bash-ism dash rejects, which kills the script on line 1); errors are caught per-command.
REVIEW_PROMPT_TEMPLATE = """You are reviewing a pull request that another agent wrote, alone \
in a VM, with nobody else looking at it. Whether it merges depends on what you report.

Repository: {repo}
Issue #{number}: {title}
Pull request: {pr_url}
You are checked out on the branch `{branch}`. The base branch is `{base}`.

--- the issue's acceptance criteria ---
{criteria}
--- end criteria ---

--- the issue body the change was written against ---
{body}
--- end issue body ---

The criteria are the contract; the body is context for reading the change. It tells you what
the step was for, where its author expected the work to land, and where its lane ended. Use it
to judge scope — do not mine it for extra gates. A criterion is the only thing that blocks on
its own; prose in the body is not one. Where the body and the criteria disagree, the criteria
win: record the disagreement in `notes` rather than acting on it.

Your job is to find out whether each criterion is true. Not whether the code looks correct,
not whether you would have written it that way — whether the criterion holds.

The rule that matters: **every verdict needs evidence you produced by running something.**
Evidence is a command and its output, a test name that passed, or a `file:line` you read. A
criterion you did not verify is `cannot_verify`, and `cannot_verify` counts the same as
`not_met`. Never mark something `met` because the code appears to do it — you have a whole VM
here, so run it instead.

How to check each criterion, by its `mode`:

- `test` — run the test named in `verify` on this branch. It must pass. Then confirm it would
  have caught the problem, by running it against the code as it was before this change:

      git rev-parse HEAD > /tmp/head.txt
      git checkout {base_sha} 2>/dev/null
      git checkout $(cat /tmp/head.txt) -- <the test file(s) from verify>
      <run the same test command>          # this MUST fail
      git checkout $(cat /tmp/head.txt)
      git checkout -B {branch} $(cat /tmp/head.txt)

  A new test that passes against the old code proves nothing: either the criterion was already
  satisfied and this change did not do it, or the test asserts nothing. Report the criterion
  `not_met` if that happens, and say which of the two it looked like. Skip this step only for
  criteria marked `regression: true`, which exist to prove old behaviour still works.
- `probe` and `structure` — run the command in `verify`. Exit status 0 is `met`, anything else
  is `not_met`. Quote the command and its output as evidence.
- `inspect` — read what `verify` points at and report what you found. This is the one mode that
  cannot block a merge, so be useful rather than cautious: say what is there and what is missing.

Also run the repo's own fast checks once, as described here:

{project_notes}

If any of them fail, that is a finding regardless of the criteria.

Then look for two specific things and report them as findings if present:

1. **Scope creep.** Map every changed file (`git diff --name-only {base_sha}...HEAD`) to a
   criterion, to the issue's `## Task`, or to its `## Where this goes` map if it has one.
   Files that map to none of those are findings. Two things about that map, when present:
   - It is **advisory**. It was written before the code existed, and the issue itself tells the
     builder to follow the repo where the two disagree. So a changed file that is not on the
     map is a reason to look closer, not a finding by itself.
   - A file that is **on** the map but was never touched belongs in `notes`: either the step is
     unfinished or the map was wrong. Both are worth knowing; neither blocks on its own.
   Anything the issue's `## Boundaries` section puts in its `Never:` lane **is** a finding if
   the change does it. That is the issue's own written rule, not an opinion you formed.
2. **Rules broken.** Check the change against the repo's own written rules — `CLAUDE.md`,
   `docs/adr/*`, `.mem/`. Only rules that are actually written down. Do not invent a
   convention and then report the code for violating it; if it is not written anywhere, it is
   not a finding.

When you are done, write your verdict to **/tmp/factory-verdict.json** and nothing else:

```json
{{
  "verdict": "approve" | "request_changes",
  "criteria": [
    {{"id": "AC1", "status": "met" | "not_met" | "cannot_verify",
      "evidence": "the command you ran and what it printed, or file:line"}}
  ],
  "findings": ["one line each — what is wrong and where"],
  "notes": ["anything advisory that should not block"]
}}
```

Include every criterion by its `id`, including `inspect` ones. Say `request_changes` if any
criterion is not met or you found something that should block; `approve` only if you actually
verified everything. Do not modify the branch, do not commit, and do not open or close
anything on GitHub — writing that file is the entirety of your output.
"""


# --------------------------------------------------------------------------- learning

# Where the learning run reads its evidence and writes its conclusions. Files rather than a
# prompt, for the reason the memory candidate queue uses one: the digest is a document the
# agent will want to grep and re-read, and a proposal is a paragraph with citations attached.
DIGEST_ENV = "FACTORY_DIGEST"
DIGEST_PATH = "/tmp/factory-digest.json"
LEDGER_ENV = "FACTORY_LEDGER"
LEDGER_PATH = "/tmp/factory-ledger.json"
PROPOSALS_PATH = "/tmp/factory-proposals.json"

# The numbers a proposal is allowed to promise to move. Closed, because "which metric" is the
# question that decides whether the change can ever be graded, and an open string field would
# fill up with one-off phrasings that no query can evaluate — at which point every row in the
# ledger is permanent by default.
LEARN_METRICS = (
    "review_rejection_rate",
    "ship_nothing_rate",
    "retry_rate",
    "cost_per_shipped_issue",
)

LEARN_PROMPT_TEMPLATE = """You are improving how agents work in one repository, by reading what \
actually happened the last time they tried.

Repository: {repo}
You are checked out on `{base}`, read-only. You will not commit anything.

Two files hold everything you are reasoning from:

- `{digest_path}` — what went wrong in the last {days} days: reviews that sent work back, runs
  that shipped nothing, failures clustered by signature, failing tool calls, what memory was
  read, which skills were loaded, what it cost. Every cluster names the runs behind it.
- `{ledger_path}` — what this loop has already changed in this repo, including what was
  rejected and what was merged and later reverted.

Read the ledger first, and read all of it. Proposing something that was tried and reverted is
the characteristic way a loop like this wastes a cycle, and the ledger is the only thing that
remembers.

--- your first job: grade what is already merged ---

For each `merged` row in the ledger that has no `observed` value, decide whether it worked.
The digest is segmented so you can compare: `outcomes` carries `context_versions`, and runs
before and after a change carry different `base_sha`. Say `kept` if the metric it named moved
in the right direction, `reverted` if it did not.

Be willing to say `reverted` about a change that sounds sensible. A rule that reads well and
changed nothing is not harmless — it is permanent context, paid for on every run, in the
budget that is already three quarters of what this factory spends. If the evidence is genuinely
too thin to say, leave it ungraded and say so in `why`; do not guess `kept` to be polite.

--- your second job: propose at most {max_proposals} changes ---

Rank ruthlessly. You are not listing what could be improved, you are choosing the few things
most worth a review cycle each. Fewer, better-evidenced proposals are the right answer, and
zero is a legitimate answer when nothing in the window justifies one.

**Ask this before anything else: was the issue the problem?**

An agent can only be as good as what it was asked for. Read the actual issues behind the
rejections and the fix cycles — `gh issue view <n> --repo {repo}` — before you conclude
anything about the agent that worked on them. Look for:

- an acceptance criterion that could not be verified as written, or that a reasonable reading
  would satisfy without doing the work;
- scope that spans two changes, so the review had to judge a diff nobody could have kept tidy;
- a `## Task` that describes an outcome but not where it goes, leaving the builder to guess
  and the reviewer to call the guess scope creep;
- an issue that took two cycles. This factory allows two deliberately, because a third
  failure means the issue is wrong rather than the code. An issue that used both is evidence
  about how it was written.

When the issue was the defect, say so with `"artifact": "compose"` and describe what the
work order should have said. That is recorded for a human and never queued — the skill that
writes issues is not in this repository — but it is the most valuable thing you can find,
and here is why it is worth a proposal slot: the alternative reading of the same evidence
produces a skill that teaches every future builder to compensate for a badly-written issue.
That skill is context, loaded on runs that did not need it, paid for forever, and the issues
go on being written the same way. Fixing the work order fixes every issue after it.

Prefer this diagnosis when the evidence is ambiguous. The 2026-08-12 analysis of this factory
found no lazy or careless agent behaviour at all — no force-pushes, no skipped tests, no
`--no-verify` — and concluded that the agent's judgment was sound and everything around it
leaked the time and money. Assume that still holds until the runs in front of you say
otherwise.

**Where a learning goes.** This is the judgment that matters most, and getting it wrong is why
rules get written that never fire:

1. **Was the work order wrong?** Then no amount of teaching the builder fixes it —
   `"artifact": "compose"`, per the section above. Ask this first, every time.
2. **Would ignoring it hang the run or burn money?** Then it is a harness invariant and prose
   cannot fix it — it needs a flag, an env var, or a change to the dispatch script. File it
   with `"artifact": "harness"`. It will be recorded and shown to a human, not built. A skill
   cannot prevent a hang, because the agent only reads a skill if it thinks to.
3. **Is it a fact about this repo?** Where something lives, what a module is for, a failure
   and its resolution → a `.mem/` record (`"artifact": "mem"`).
4. **Is it a way of working in this repo?** A procedure the agent should follow when it finds
   itself in a particular situation → a repo skill in `.claude/skills/`
   (`"artifact": "skill"`). Loaded on demand, so it costs nothing on the runs that do not
   need it.

   **Before proposing one, run `ls ~/.claude/skills` and make sure the name is not already
   there.** A personal skill overrides a project one — that direction, which is the opposite
   of what "more specific wins" would suggest — and this VM has the shared skills installed
   personally. So a repo skill that reuses a global name is not overridden loudly, it is
   silently never loaded: the issue merges, the file sits in the repo, no run ever reads it,
   and the next learning run sees a skill with zero loads and proposes deleting it. Nothing
   in that sequence looks like a mistake. Pick a name that does not collide, and say in the
   issue body which names you checked against.
5. **Must it be known from turn 0, on every single run?** Only then `.factory.md`
   (`"artifact": "factory_md"`). That file is spliced into every prompt this repo ever runs,
   so a line added there is paid for forever, whether or not it is relevant. The bar is high
   and most things do not clear it.

**The failure this repository's data is best at revealing.** Check for it explicitly. Take each
recurring cluster in the digest and look in `.mem/` for a record that already documents it. If
one exists and the failure kept happening, the record is not the problem — *retrieval* is. Do
not propose writing the learning again. Look at the record's entry in `.mem/index.jsonl`: its
`files` list is the only thing priming matches against, so a path missing from it takes the
record out of range entirely. Propose fixing the index entry, or moving the learning to a place
that does not depend on retrieval at all. A second copy of a record nobody found is two records
nobody will find.

**The candidate queue is already written evidence.** The digest's `candidates` section holds
learnings earlier agents noticed but were not confident enough to commit. Nothing writes them
into `.mem/` — accepting one records a verdict and stops there, deliberately, because the thing
that writes it is a later agent. You are that agent. A candidate filed repeatedly by different
runs is the repo stating something about itself more plainly than any single failure does;
promoting one is often a better-evidenced proposal than anything you would derive yourself.

**Deleting is proposing.** Compare the skills in `.claude/skills/` against the digest's
`skills` list, which is every skill the window's runs actually loaded. A skill in the repo and
absent from that list was not read — that is an observation, not an opinion, and a `delete`
proposal is the correct response to it. One check first, because it is the one case where the
observation is misleading: if a skill of the same name exists in `~/.claude/skills`, the repo
one was shadowed rather than ignored, and the fix is to rename it rather than to delete it.
Deletions count against your {max_proposals} the same
as additions. They are usually the highest-value thing you can do: everything in `.factory.md`
and every loaded skill is context, context is cache reads, and cache reads are most of what a
run costs.

--- the rules your proposals must satisfy ---

**Evidence or nothing.** Every proposal cites `run_ids` from the digest. These are checked
against the database before anything is filed, and a proposal citing a run that does not exist
in this repo is discarded — so cite runs you actually read, and do not reconstruct an id from
memory.

**Falsifiable.** Every proposal names one `metric` from this list — {metrics} — and the
`baseline` it stands at now, from the digest. If you cannot say what would show the change
worked, you do not understand the problem well enough to propose a fix for it yet.

**Buildable.** `body` is a complete issue for another agent who has not seen any of this. It
must state the change, where it goes, and end with an `## Acceptance criteria` block in the
same YAML shape this repo's other issues use, so the change is reviewed like any other. Write
criteria that can be executed, not admired.

--- output ---

Write `{proposals_path}` and nothing else. No commits, no branches, no issues — the control
plane files these. Exactly this shape, raw JSON, no fence:

{{
  "gradings": [
    {{"id": "<ledger row id>", "verdict": "kept" | "reverted",
      "observed": <number>, "why": "<one sentence, citing what moved>"}}
  ],
  "proposals": [
    {{"artifact": "compose" | "skill" | "mem" | "factory_md" | "harness",
      "action": "add" | "edit" | "delete" | "revert",
      "target": "<path the change lands in>",
      "title": "<issue title>",
      "body": "<the full issue, ending in ## Acceptance criteria>",
      "rationale": "<why this, in one or two sentences a human will read in six weeks>",
      "evidence": {{"run_ids": ["..."], "signature": "<the cluster this came from>"}},
      "metric": "<one of the metrics above>",
      "baseline": <the number now>}}
  ]
}}

An empty `proposals` list is a real answer and a better one than a padded list. If the window
holds nothing worth changing, say so by writing `{{"gradings": [...], "proposals": []}}`.
"""


# The setup both dispatch scripts open with: get a checkout of the assigned repo, make git
# willing to work in a directory it did not create, say who is committing, and get the remote
# refs. It lives in one constant because both scripts need it identical — a review VM that
# resolves the branch differently from the build VM that wrote it reviews something nobody
# built.
#
# The run brings its own repo. `golden-copy` carries tooling and auth and no checkout at all,
# so this clones — and the warm tier is the same script taking the other branch: a
# `golden-<repo-slug>` with the repo already in place skips the clone, which is all "warm" has
# ever meant. Writing it as one script rather than two is what lets a repo be dispatched before
# it has been provisioned.
#
# `--filter=blob:none` rather than `--depth 1`: reviews need real ancestry (they diff against
# a merge base) and agents run `git log`, but the file contents of history nobody reads can be
# fetched on demand.
PRELUDE = r"""
[ -n "$FACTORY_REPO" ] || { echo "FACTORY: no repo assigned" >&2; exit 90; }
workdir="${FACTORY_WORKDIR:-$HOME/work}"
dir="$workdir/${FACTORY_REPO##*/}"

# The pre-clone override, kept for one release: a golden built the old way holds its single
# checkout at FACTORY_REPO_DIR. Taken only when it really is a checkout of the assigned repo,
# because the alternative — working in whatever repo that directory happens to hold — is a run
# that pushes one repo's branch into another.
if [ -n "$FACTORY_REPO_DIR" ] && [ -d "$FACTORY_REPO_DIR/.git" ] && \
   git -C "$FACTORY_REPO_DIR" remote get-url origin 2>/dev/null | grep -qi "$FACTORY_REPO"; then
  dir="$FACTORY_REPO_DIR"
fi

if [ -d "$dir/.git" ]; then
  echo "FACTORY: reusing the checkout at $dir"
else
  echo "FACTORY: cloning $FACTORY_REPO into $dir"
  mkdir -p "$workdir" || { echo "FACTORY: cannot create $workdir" >&2; exit 90; }
  gh repo clone "$FACTORY_REPO" "$dir" -- --filter=blob:none \
    || { echo "FACTORY: clone of $FACTORY_REPO failed" >&2; exit 90; }
fi

cd "$dir" || { echo "FACTORY: workspace unusable: $dir" >&2; exit 90; }
git config --global --add safe.directory "$dir" 2>/dev/null || true
git config user.name  "software-factory" 2>/dev/null || true
git config user.email "factory@users.noreply.github.com" 2>/dev/null || true
echo "FACTORY: fetching origin"
git fetch --prune origin || { echo "FACTORY: git fetch failed" >&2; exit 91; }
"""

# Fail fast when the machine's toolchain does not match what the repo pins. This is not
# cosmetic: two npm majors disagree about what belongs in a lock file, so a mismatched golden
# has its agents silently write lock files CI cannot install — which surfaces two steps later
# as an unrelated-looking CI failure rather than as the version problem it is. That exact
# mismatch killed CI on fourteen consecutive commits before anyone noticed. Skipped when the
# repo pins nothing or the runtime is absent, so this stays harmless for non-Node projects.
#
# Shared by both scripts but deliberately not part of PRELUDE: it reads the pin out of the
# working tree, so it has to run after each script has checked its own branch out. Read the pin
# before the checkout and you assert against whatever commit the machine happened to be sitting
# on, which is exactly the stale answer this guard exists to catch.
#
# It repairs before it refuses. A warm golden is pre-matched to its own repo's pin, but the
# base image serves every repo the deployment watches and they do not agree on a Node version.
# So on the base a mismatch is ordinary and the fix is to install what the repo asks for. Only a mismatch that survives the install is fatal — and the old advice,
# "the golden needs rebuilding", is no longer the right sentence for it.
NODE_GUARD = r"""
if [ -f .nvmrc ] && command -v node > /dev/null 2>&1; then
  want=$(tr -dc '0-9.' < .nvmrc | cut -d. -f1)
  have=$(node -v | tr -d 'v' | cut -d. -f1)
  if [ -n "$want" ] && [ "$want" != "$have" ] && command -v fnm > /dev/null 2>&1; then
    echo "FACTORY: node $have is not the pinned $want; installing it"
    eval "$(fnm env 2>/dev/null)" 2>/dev/null || true
    fnm use --install-if-missing > /dev/null 2>&1 || true
    have=$(node -v | tr -d 'v' | cut -d. -f1)
  fi
  if [ -n "$want" ] && [ "$want" != "$have" ]; then
    echo "FACTORY: node $have does not match the pinned $want and could not be repaired" >&2
    exit 93
  fi
fi
"""

VM_SCRIPT = PRELUDE + r"""
# When there is earlier work on this issue, resume the branch it was pushed to rather than
# resetting to the base and throwing it away. The VM is always fresh; the branch is what
# carries work forward. Only then: a genuinely first dispatch starts from the base, so
# re-queueing an issue whose branch is still lying around from an earlier merged PR gets a
# clean start, not a resurrection.
#
# The control plane decides which of those this is and says so in one variable, because it is
# the only side that knows both counters. This used to test the attempt counter directly, and
# a fix run opened by a review is attempt 1 of its cycle — so keying on the attempt alone
# would reset the branch to the base and discard the very commits the reviewer just approved.
if [ "$FACTORY_RESUME" = "1" ] && git rev-parse --verify --quiet "origin/$FACTORY_BRANCH" > /dev/null; then
  echo "FACTORY: resuming $FACTORY_BRANCH from origin/$FACTORY_BRANCH"
  git checkout -B "$FACTORY_BRANCH" "origin/$FACTORY_BRANCH" || { echo "FACTORY: checkout failed" >&2; exit 92; }
else
  echo "FACTORY: checking out $FACTORY_BRANCH from origin/$FACTORY_BASE"
  git checkout -B "$FACTORY_BRANCH" "origin/$FACTORY_BASE" || { echo "FACTORY: checkout failed" >&2; exit 92; }
fi
""" + NODE_GUARD + r"""
echo "FACTORY: starting agent"
# The rollback for this step, and nothing more: a golden captured before the wrapper existed
# still launches the way the control plane used to launch it, and exits with the agent's own
# status. It goes away once every golden carries factory-agent.
command -v factory-agent >/dev/null 2>&1 || { claude -p "$FACTORY_PROMPT" \
  --effort "$FACTORY_AGENT_EFFORT" \
  --disallowed-tools Agent Task ScheduleWakeup \
  --dangerously-skip-permissions \
  --output-format stream-json --verbose < /dev/null; exit $?; }
echo "FACTORY-MANIFEST $(tr -d '\n' < /etc/factory/agent.json 2>/dev/null)"
exec factory-agent
"""


def dispatch_env(
    repo: str,
    branch: str,
    base: str,
    prompt: str,
    run_id: str,
    number: int,
    vm_name: str,
    resume: bool | None = None,
    kind: str = "build",
) -> dict:
    """Everything a run VM is told, for either kind of run.

    One builder because the two paths must agree: a review VM that clones a different repo,
    or authenticates as somebody else, than the build VM whose work it is checking is not
    reviewing that work. The differences between them are the two arguments — a build run
    says whether to resume the branch, a review run labels itself in the trace — and
    everything else is shared by construction rather than by two people remembering to edit
    both.

    `resume` is a decision, not a counter. Whether there is earlier work on the branch depends
    on both the attempt and the review cycle, and only the caller holds both, so it answers
    the question here instead of shipping the numbers and having the shell reason about them.
    `None` is the review path, which never checks out anything of its own to continue.

    `GH_TOKEN` is the control plane's own durable credential and covers the clone, the push
    and `gh pr create` from one source. The golden's `gh` login stays as the fallback for a
    deployment that has not set one, which is why an empty token is left out entirely rather
    than exported as an empty string that would shadow it.
    """
    env = {
        # Which repo this run is for. The golden no longer knows.
        "FACTORY_REPO": repo,
        "FACTORY_WORKDIR": settings.workdir,
        # The pre-clone override, honoured only when it holds this repo. See PRELUDE.
        "FACTORY_REPO_DIR": settings.repo_dir,
        "FACTORY_BRANCH": branch,
        "FACTORY_BASE": base,
        "FACTORY_PROMPT": prompt,
        "FACTORY_AGENT_EFFORT": settings.agent_effort,
        # How long a single shell command may run before the agent's own tooling moves it to
        # the background. The default (120s) is shorter than an ordinary build or test run
        # here, and a backgrounded command is what the agent then waits on forever — in
        # headless mode (`claude --print`) there is no loop to deliver the notification it
        # expects. Generous values keep everything legitimate in the foreground; a genuinely
        # stuck command is bounded by FACTORY_RUN_TIMEOUT.
        "BASH_DEFAULT_TIMEOUT_MS": str(settings.bash_default_timeout * 1000),
        "BASH_MAX_TIMEOUT_MS": str(settings.bash_max_timeout * 1000),
        # Correlation key: every run carries its id so traces can attach without changing
        # the dispatch contract later. Anything that is not a build says which kind it is —
        # written from the argument rather than as one hard-coded `review` case, so a kind
        # added later is labelled without anyone remembering to come back here.
        "OTEL_RESOURCE_ATTRIBUTES": (
            f"run.id={run_id},issue={repo}#{number},repo={repo},vm={vm_name}"
            + (f",kind={kind}" if kind != "build" else "")
        ),
    }
    if resume is not None:
        env["FACTORY_RESUME"] = "1" if resume else "0"
    if kind == "build":
        # Where to propose learnings, read back before the VM is reaped. Set only on the path
        # that asks for it and reads it: an environment variable naming a file nobody collects
        # is an invitation to write into a void, and no other kind's prompt makes that
        # promise — review and plan promise nothing, and a learning run proposes through its
        # own file, which the control plane validates; `_collect_memory_candidates` runs on
        # the build path alone.
        env[MEMORY_CANDIDATE_ENV] = MEMORY_CANDIDATE_PATH
    if settings.github_token:
        env["GH_TOKEN"] = settings.github_token
    # Durable auth for the agent itself, overriding the golden's expiring OAuth.
    if settings.anthropic_api_key:
        env["ANTHROPIC_API_KEY"] = settings.anthropic_api_key
    if settings.claude_code_oauth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = settings.claude_code_oauth_token
    return env


# A shell name, so a key that could not be one is never pasted into a command.
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def export_prelude(env: dict) -> str:
    """Shell that turns the assignments `stream_exec(env=...)` makes into a real environment.

    boxd's wire protocol has no env field. Its SDK says so and works around it by prefixing
    the command with `K=v` assignments (`boxd.resources.machines._exec_init`), which is
    correct for the one-line command that comment has in mind and quietly wrong for a script.
    In front of a *multi-line* command the prefix lands on a line of its own, and a line of
    bare assignments sets shell variables rather than exporting them. The script itself then
    reads `$FACTORY_REPO` perfectly well while every process it starts inherits none of them.

    That is not a theoretical gap. It is what stopped the factory on 2026-08-19: `gh repo
    clone` could not see `GH_TOKEN`, failed with "please run gh auth login", and the prelude
    exited 90 before the agent ever started — while the same log line printed the repo name
    it had just read from the very variable `gh` could not see, which is what made it look
    like the environment had arrived.

    Names come from the dict, so a variable added to `dispatch_env` cannot be forgotten here.
    """
    names = [k for k in env if _ENV_NAME.fullmatch(k)]
    if not names:
        return ""
    # Leading newline so the SDK's prefix stays a line of pure assignments; without it the
    # assignments would attach to this `export` as a command prefix and the fix would depend
    # on how the shell scopes assignments to a special builtin.
    return "\nexport " + " ".join(names) + "\n"


# --------------------------------------------------------------------------- manifest

# What a golden says about itself, on one line, immediately before it becomes the agent.
# The prefix is what lets the stream parser tell the announcement from the agent's own
# output without guessing: every other line on stdout belongs to the agent.
MANIFEST_PREFIX = "FACTORY-MANIFEST"

# Where a transcript lives when the manifest names no path. Claude Code's own layout, kept
# as the default so a golden built before any of this still gives its transcript up.
CLAUDE_TRANSCRIPT_GLOB = '"$HOME"/.claude/projects/*/*.jsonl'


def parse_manifest(line: str) -> dict:
    """The manifest a golden announced, or `{}` when the line does not carry a usable one.

    Never raises, for the same reason the transcript salvage is best-effort: the manifest
    says where the transcript is and which agent to credit, and a run whose real work
    succeeded must not be failed by a hand-edited `/etc/factory/agent.json`, a wrapper that
    forgot to strip the file's newlines, or an image that echoed the prefix and nothing
    after it. `{}` is a complete answer — every reader of a manifest has a default.
    """
    _, _, payload = str(line or "").partition(MANIFEST_PREFIX)
    try:
        manifest = json.loads(payload)
    except (TypeError, ValueError):
        return {}
    return manifest if isinstance(manifest, dict) else {}


def transcript_glob(manifest: dict | None = None) -> str:
    """The shell glob that finds the agent's session transcript inside the VM.

    The manifest wins when it names one, because only the golden knows where its agent
    writes; an agent that is not Claude Code does not keep a `~/.claude` directory at all.
    """
    named = (manifest or {}).get("transcript")
    return named.strip() if isinstance(named, str) and named.strip() else CLAUDE_TRANSCRIPT_GLOB


# --------------------------------------------------------------------------- log


VERDICT_PATH = "/tmp/factory-verdict.json"

# Review runs check out the PR branch and change nothing. No `-B` from the base, no push: a
# reviewer that can write to the branch is a reviewer that can make its own findings go away.
REVIEW_SCRIPT = PRELUDE + r"""
rm -f /tmp/factory-verdict.json
echo "FACTORY: checking out $FACTORY_BRANCH for review"
git checkout -B "$FACTORY_BRANCH" "origin/$FACTORY_BRANCH" || { echo "FACTORY: checkout failed" >&2; exit 92; }
""" + NODE_GUARD + r"""
echo "FACTORY: starting reviewer"
command -v factory-agent >/dev/null 2>&1 || { claude -p "$FACTORY_PROMPT" \
  --effort "$FACTORY_AGENT_EFFORT" \
  --disallowed-tools Agent Task ScheduleWakeup \
  --dangerously-skip-permissions \
  --output-format stream-json --verbose < /dev/null; exit $?; }
echo "FACTORY-MANIFEST $(tr -d '\n' < /etc/factory/agent.json 2>/dev/null)"
exec factory-agent
"""


# Learning runs read the base branch and change nothing on it. Same shape as the reviewer for
# the same reason: a run with no reason to push must not be able to.
#
# The digest and ledger arrive as environment variables and are written to disk here. They
# could have been spliced into the prompt, but they are documents — the agent greps them,
# re-reads them, and cites out of them — and a document pasted into a prompt is one the agent
# can only remember rather than consult.
LEARN_SCRIPT = PRELUDE + r"""
rm -f /tmp/factory-proposals.json
echo "FACTORY: checking out $FACTORY_BASE to learn from"
git checkout -B "$FACTORY_BASE" "origin/$FACTORY_BASE" || { echo "FACTORY: checkout failed" >&2; exit 92; }
printf '%s' "$FACTORY_DIGEST" > /tmp/factory-digest.json || { echo "FACTORY: could not write digest" >&2; exit 93; }
printf '%s' "$FACTORY_LEDGER" > /tmp/factory-ledger.json || { echo "FACTORY: could not write ledger" >&2; exit 93; }
unset FACTORY_DIGEST FACTORY_LEDGER
""" + NODE_GUARD + r"""
echo "FACTORY: starting analyst"
command -v factory-agent >/dev/null 2>&1 || { claude -p "$FACTORY_PROMPT" \
  --effort "$FACTORY_AGENT_EFFORT" \
  --disallowed-tools Agent Task ScheduleWakeup \
  --dangerously-skip-permissions \
  --output-format stream-json --verbose < /dev/null; exit $?; }
echo "FACTORY-MANIFEST $(tr -d '\n' < /etc/factory/agent.json 2>/dev/null)"
exec factory-agent
"""


def valid_proposals(payload: dict | None, known_runs: set[str], limit: int) -> list[dict]:
    """The proposals in `payload` that are complete, well-formed and actually cited.

    Pure and total: anything malformed is dropped rather than raised on, because this reads a
    file an agent wrote and the failure mode to avoid is one bad entry costing the whole run's
    output.

    `known_runs` is what makes "evidence or nothing" a rule instead of a request. The prompt
    asks for run ids; this checks them against the runs the digest was actually built from, so
    a citation the agent reconstructed from memory — or invented — cannot enter the ledger.
    That check is the whole reason proposals come back as a file for the control plane to file,
    rather than as issues the agent opens itself: a fence in a prompt is a suggestion.

    Trimmed to `limit` after filtering, not before, so a run that files one malformed proposal
    does not lose a good one to the cap.
    """
    if not isinstance(payload, dict):
        return []
    out = []
    for item in payload.get("proposals") or []:
        if not isinstance(item, dict):
            continue
        artifact = str(item.get("artifact") or "")
        action = str(item.get("action") or "")
        metric = str(item.get("metric") or "")
        title = str(item.get("title") or "").strip()
        body = str(item.get("body") or "").strip()
        rationale = str(item.get("rationale") or "").strip()
        if artifact not in db.IMPROVEMENT_ARTIFACTS or action not in db.IMPROVEMENT_ACTIONS:
            continue
        if metric not in LEARN_METRICS or not title or not body or not rationale:
            continue
        evidence = item.get("evidence")
        if not isinstance(evidence, dict):
            continue
        cited = [str(r) for r in (evidence.get("run_ids") or []) if str(r) in known_runs]
        if not cited:
            continue
        baseline = item.get("baseline")
        out.append({
            "artifact": artifact,
            "action": action,
            "target": str(item.get("target") or "")[:300] or None,
            "title": title[:250],
            "body": body,
            "rationale": rationale[:2000],
            "evidence": json.dumps({
                "run_ids": cited,
                "signature": str(evidence.get("signature") or "")[:300],
            }),
            "metric": metric,
            "baseline": float(baseline) if isinstance(baseline, (int, float)) else None,
        })
    return out[:limit]


def decide(verdict: dict | None, criteria: list[dict]) -> tuple[bool, str, list[str]]:
    """Turn a reviewer's verdict into a merge decision. Returns (approved, why, findings).

    The control plane decides, not the agent. The agent reports per-criterion status with
    evidence; this recomputes the outcome from those statuses, so an "approve" cannot survive a
    criterion the agent itself marked `not_met`. It can still request changes for something
    outside the criteria — a reviewer may block for a reason nobody thought to write down, but
    it may not wave one through.

    Fails closed everywhere: no verdict file, unparseable JSON, or a criterion the reviewer
    simply didn't mention all mean "not approved".
    """
    if not isinstance(verdict, dict):
        return False, "no usable verdict from the reviewer", []

    findings = [str(f) for f in (verdict.get("findings") or [])][:20]
    reported = {
        str(c.get("id")): c
        for c in (verdict.get("criteria") or [])
        if isinstance(c, dict) and c.get("id")
    }

    blocking = [c for c in criteria if c.get("mode") in BLOCKING_MODES]
    unmet, missing = [], []
    for criterion in blocking:
        cid = str(criterion["id"])
        got = reported.get(cid)
        if got is None:
            missing.append(cid)
        elif got.get("status") != "met":
            unmet.append(f"{cid} {got.get('status', 'unknown')}")

    if missing:
        return False, f"reviewer did not report on {', '.join(missing)}", findings
    if unmet:
        return False, f"criteria not met: {', '.join(unmet)}", findings
    if verdict.get("verdict") != "approve":
        return False, "reviewer requested changes", findings
    return True, f"{len(blocking)} criteria met", findings


def merge_outcome(auto_merge: bool, merged: bool, ci_failure: str | None) -> str:
    """What an approved review should do with the pull request: "done", "fix" or "human".

    Kept separate from the code that acts on it because the interesting part is the routing,
    not the doing, and the routing is where this went wrong: every ending that was not a
    merge used to be labelled `agent:done`, which left broken pull requests sitting open
    under issues that claimed to be finished.

    - "done"  — merged, or auto-merge is off and a human was always going to take it from here
    - "fix"   — CI ran and came back red, which is a defect a fix run can be sent back for
    - "human" — not merged and no failure to work from: pending, unreachable, or refused
    """
    if merged or not auto_merge:
        return "done"
    return "fix" if ci_failure else "human"


class RunLog:
    """Append-only, human-readable log for one run. The UI tails this file over SSE."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = path.open("a", encoding="utf-8", buffering=1)

    def write(self, line: str) -> None:
        self._fh.write(line.rstrip("\n") + "\n")

    def close(self) -> None:
        try:
            self._fh.close()
        except OSError:
            pass


def _format_system(event: dict) -> list[str]:
    if event.get("subtype") != "init":
        return []
    return [f"[agent] session {event.get('session_id', '?')} started"]


def _format_assistant(event: dict) -> list[str]:
    lines: list[str] = []
    for block in event.get("message", {}).get("content", []) or []:
        btype = block.get("type")
        if btype == "text":
            text = (block.get("text") or "").strip()
            if text:
                lines.extend(text.splitlines())
        elif btype == "tool_use":
            name = block.get("name", "tool")
            supplied = block.get("input", {}) or {}
            hint = (
                supplied.get("command")
                or supplied.get("file_path")
                or supplied.get("pattern")
                or supplied.get("skill")
                or ""
            )
            hint = str(hint).replace("\n", " ")
            lines.append(f"[tool] {name}{': ' + hint[:160] if hint else ''}")
    return lines


def _format_user(event: dict) -> list[str]:
    return [
        "[tool] -> error"
        for block in event.get("message", {}).get("content", []) or []
        if block.get("type") == "tool_result" and block.get("is_error")
    ]


def _format_finished(event: dict) -> list[str]:
    usage = event.get("usage", {}) or {}
    tokens = ""
    if usage:
        tokens = (
            f" | tokens in={usage.get('input_tokens', '?')} "
            f"out={usage.get('output_tokens', '?')}"
        )
    return [
        f"[agent] finished: {event.get('subtype', 'done')}"
        f" | {event.get('num_turns', '?')} turns{tokens}"
    ]


# Which event types this log knows how to read, and how. A table rather than a chain of
# comparisons for one reason: whether an event is *recognised* is now a question the
# stream has to ask, so that an event from a runtime whose shape nobody has written a
# formatter for is logged raw instead of vanishing (see `stream_lines`). Every key here
# is Claude Code's vocabulary — a second runtime adds its own names, and nothing outside
# this table has to change.
FORMATTERS = {
    "system": _format_system,
    "assistant": _format_assistant,
    "user": _format_user,
    "result": _format_finished,
}

# How much of an unrecognised event goes into the log. Enough to see what it was; not so
# much that one chatty runtime buries the run in its own protocol.
RAW_EVENT_MAX = 300


def format_event(event: dict) -> list[str]:
    """Turn one agent event into readable log lines, or none if it says nothing.

    Written defensively: the event schema is not a stable contract, so an event this
    does not recognise produces nothing rather than raising. `stream_lines` is what
    decides such an event is worth a raw line anyway.
    """
    formatter = FORMATTERS.get(event.get("type"))
    return formatter(event) if formatter else []


def stream_lines(event: dict, raw: str) -> list[str]:
    """What one JSON line from the agent contributes to the run log.

    The fallback is the whole point. An agent whose events no formatter recognises used
    to stream a *completely empty* log to the UI while working perfectly — the failure
    that hides itself at exactly the moment somebody is watching a new agent to see
    whether it works. A truncated raw line is unlovely and always better than silence.

    A recognised event that produces no lines stays silent, because that is a formatter
    saying "nothing here worth reading", not an unread event: half of a normal run is
    tool results nobody needs to see.
    """
    if event.get("type") in FORMATTERS:
        return format_event(event)
    raw = raw.strip()
    return [f"[agent] {raw[:RAW_EVENT_MAX]}{'…' if len(raw) > RAW_EVENT_MAX else ''}"]


# --------------------------------------------------------------------------- run


@dataclass(frozen=True)
class MergeAttempt:
    """What came of trying to merge one pull request.

    A record rather than a tuple because it carries four facts that are easy to confuse, and
    two of them are only meaningful in one ending each:

    - `merged`     — did it land
    - `ci_failure` — set *only* when checks ran and came back red, which is the one ending a
                     fix run can act on. Everything else leaves it None, because "we could
                     not find out" and "CI says no" call for different responses.
    - `why`        — always populated, in plain language, for the issue comment and the runs
                     table. This used to be dropped for every non-CI ending, which is how a
                     stranded pull request came to be recorded as "cause unknown" when GitHub
                     had in fact said exactly what was wrong.
    - `head_sha`   — the commit the checks were verified on, so the caller can fetch that
                     commit's failing logs rather than whatever is at the head by then.
    - `ci_log`     — the failing checks' logs, fetched once while recording the CI phase and
                     carried here so the fix prompt does not go and fetch the same thing a
                     second time. None whenever there was nothing red to read.
    """

    merged: bool
    ci_failure: str | None
    why: str
    head_sha: str | None = None
    ci_log: str | None = None


async def _record_ci(
    repo: str,
    issue: dict,
    branch: str,
    cycle: int,
    pr_url: str,
    sha: str | None,
    green: bool,
    why: str,
    failed: list[str],
    started_at: str,
    log: RunLog,
) -> str | None:
    """Write what CI decided into the run log as a phase of its own, and return its log text.

    CI was the one thing that judged a run and left no trace of its own. Its verdict reached
    the database as a string on the *review's* `error` column, and its log reached nothing but
    the prompt of the fix run that followed — so the dispatch log could show a build reading
    `succeeded` above the review that sent it back, and answering "red on what?" meant leaving
    the factory for the Actions API. A red check is an outcome of the work like any other, so
    it gets a row like any other.

    Not a dispatch. No VM, no agent, no tokens, no cost, and those columns stay NULL rather
    than 0 — zero would read as "this ran and was free" instead of "this never ran anywhere".
    `kind` is what tells the two apart, and the UI reads it to know which fields to show.

    Best effort throughout: a run is never failed over its own bookkeeping. A record that
    could not be written costs a row in a log; a raised exception here would cost the merge.
    """
    detail = None
    try:
        run_id = uuid.uuid4().hex
        log_path = settings.log_dir / f"{run_id}.log"
        lines = [
            f"[ci] pull request {pr_url}",
            f"[ci] commit {sha or '(unknown)'}",
            f"[ci] {why}",
        ]
        if failed:
            lines.append(f"[ci] failed: {', '.join(failed)}")
        if failed and sha:
            # Fetched here, once, and handed back to the caller. The fix prompt used to make
            # this same call for itself a moment later, which is two round trips for one
            # answer and — worse — two answers, since the second one reads whatever the head
            # is by then rather than the commit these checks actually judged.
            detail = await github.failing_check_logs(repo, sha)
            log.write(f"[factory] fetched {len(detail)} chars of failing check log")
            lines += ["", detail]
        log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        await db.create_run(
            id=run_id,
            repo=repo,
            issue_number=issue["number"],
            issue_title=issue.get("title"),
            branch=branch,
            kind="ci",
            # A commit is judged once per cycle, so there is nothing here for `attempt` to
            # count. It stays 1 for the same reason a review's does.
            attempt=1,
            cycle=cycle,
            status="succeeded" if green else "failed",
            error=None if green else why,
            pr_url=pr_url,
            log_path=str(log_path),
            created_at=started_at,
            started_at=started_at,
            finished_at=db.utcnow(),
        )
    except Exception as exc:  # noqa: BLE001 - see the docstring: bookkeeping never fails a run
        log.write(f"[factory] could not record the CI phase: {exc!r}")
    return detail


async def _merge(
    repo: str,
    pr_url: str,
    base: str,
    log: RunLog,
    *,
    issue: dict,
    branch: str,
    cycle: int = 1,
) -> MergeAttempt:
    """Merge a PR once its checks are green. Never raises — a merge we skip is always safer
    than one we force, and the PR simply stays open for a human.

    The merge itself retries while GitHub is unavailable (see `github.merge_pr`) and is
    pinned to the sha the checks passed on, so "auto-merge once the tests are done" holds
    even across an outage, and can still only ever land the commit that was tested.

    Also the one place the factory ever learns a CI verdict, which is why recording that
    verdict lives here rather than at either call site: a phase that is written in one branch
    of one caller is a phase that is missing from the other.

    Runs at most twice. A merge GitHub refuses *as conflicted* is repaired by merging the base
    back into the branch where the repo's own merge drivers apply (`github.merge_base_into_branch`)
    and then asked again — and asked from the top, checks included, because reconciling moves
    the head and the whole point of pinning to a verified sha is that no other commit may land.
    Twice and no more: if a merge conflicts again after a successful reconcile, something is
    moving underneath this that a third attempt would only race with.
    """
    try:
        pr_number = int(pr_url.rstrip("/").split("/")[-1])
        sha: str | None = None
        green, why, failed = True, "checks not required", []
        # Bound before the branch below: with `merge_require_checks` off nothing is fetched
        # and nothing is recorded, and both return paths still read this.
        ci_log: str | None = None
        for reconciled in (False, True):
            started_at = db.utcnow()
            if settings.merge_require_checks:
                # The merge API waits for nothing, so without this the PR is merged seconds
                # after `gh pr create` — before CI has even started. Every run forks from main,
                # so a red main propagates into all of them.
                log.write(f"[factory] waiting for checks on PR #{pr_number}")
                sha = await github.pr_head_sha(repo, pr_number)
                green, why, failed = await github.checks_green(
                    repo, sha, timeout=settings.merge_check_timeout
                )
                ci_log = await _record_ci(
                    repo, issue, branch, cycle, pr_url, sha, green, why, failed, started_at, log
                )
            if not green:
                log.write(f"[factory] not merging PR #{pr_number}, left open: {why}")
                return MergeAttempt(False, why if failed else None, why, sha, ci_log)
            try:
                await github.merge_pr(repo, pr_number, sha=sha)
            except Exception as exc:  # noqa: BLE001 - re-raised below unless it is repairable
                if reconciled or not github.is_merge_conflict(exc):
                    raise
                log.write(f"[factory] PR #{pr_number} is conflicted; reconciling {branch}")
                ok, detail = await github.merge_base_into_branch(repo, branch, base, log.write)
                if not ok:
                    log.write(f"[factory] could not reconcile {branch}: {detail}")
                    raise
                continue
            log.write(f"[factory] merged PR #{pr_number} into {base} ({why})")
            # If this issue was the improvement loop's own proposal, it is now live. Recorded
            # here for the reason the CI verdict is: this is the one place the factory learns a
            # merge happened, and a transition written into one branch of one caller is one the
            # other caller silently skips.
            await advance_improvement(
                repo, issue.get("number") or 0, "merged", log, pr_url=pr_url
            )
            return MergeAttempt(True, None, why, sha, ci_log)
        # Unreachable — the second pass returns or raises — and spelled out anyway, because
        # falling out of the loop would return None into code that reads `.merged` off it.
        return MergeAttempt(False, None, "the merge loop ended without a decision", sha, ci_log)
    except Exception as exc:  # noqa: BLE001
        # Keep the reason. GitHub nearly always says what was wrong, and throwing that away
        # left a human with an unmerged pull request and nothing to go on.
        detail = " ".join(str(exc).split())[:200] or type(exc).__name__
        why = f"the merge call failed — {detail}"
        log.write(f"[factory] merge failed (PR left open): {exc!r}")
        return MergeAttempt(False, None, why, None)


async def _fix_cycle(
    repo: str,
    number: int,
    cycle: int,
    log: RunLog,
    *,
    reason: str,
    detail: str,
    going_back: str,
    giving_up: str,
) -> None:
    """Send a pull request back for another pass, or stop if the cycle budget is spent.

    Both things that can send one back — a reviewer that requested changes, and a CI run that
    came back red — want identical mechanics: reuse the branch so the fix builds on commits
    already pushed rather than starting over, carry the reason into the next prompt, and cap
    how many times the factory may try again unsupervised. Only the wording differs, so only
    the wording is a parameter.
    """
    if cycle < settings.max_review_cycles:
        log.write(f"[factory] {reason}; fix run {cycle + 1} scheduled")
        await _mirror_issue(repo, number, None, [], log, comment=going_back)
        # `attempt=1`, not `cycle + 1`. A fix run is the first dispatch of a new pass over
        # the pull request, not the second try at a build that failed — the build it follows
        # succeeded. Passing the cycle number as the attempt number spent the crash-retry
        # budget on review cycles and made the runs list say "try 2" about work whose first
        # try was fine. The two budgets are separate and both still cap this: at most
        # `max_review_cycles` passes, each with at most `max_attempts` dispatches.
        await create(
            repo,
            number,
            attempt=1,
            prior_error=reason,
            prior_log=detail,
            review_cycle=cycle + 1,
        )
    else:
        log.write(f"[factory] {reason}; cycle budget spent, halting")
        await _mirror_issue(
            repo,
            number,
            github.LABEL_BLOCKED,
            [github.LABEL_RUNNING, github.LABEL_QUEUED],
            log,
            comment=giving_up,
        )


async def _fail_run(
    run_id: str,
    repo: str,
    issue: dict,
    attempt: int,
    reason: str,
    log: RunLog,
    pr_url: str | None = None,
    review_cycle: int = 1,
    retryable: bool = True,
) -> None:
    """Mark a run failed, then either schedule a retry or halt the issue.

    Retry ceiling is `settings.max_attempts`. The retry is created *before* this run is
    marked terminal, so the repo never looks idle to the poller in the gap — which would let
    the next issue in the sequence jump the queue.

    `retryable=False` halts the issue whatever the attempt count says. A run stopped for
    spending past its ceiling is the case that needs it: the budget exists to bound what one
    issue can cost, and a ceiling that permits two more attempts is a ceiling of three times
    the number written in the config.
    """
    number = issue["number"]
    scheduled = False
    if retryable and attempt < settings.max_attempts:
        try:
            await create(
                repo,
                number,
                attempt=attempt + 1,
                prior_error=reason,
                prior_log=_log_tail(run_id),
                # Carried, not defaulted. Without it a crash inside fix cycle 2 came back as
                # a cycle-1 run, and the review that followed it would have been allowed a
                # fix cycle the budget had already spent.
                review_cycle=review_cycle,
            )
            scheduled = True
        except Exception as exc:  # noqa: BLE001 - can't retry -> fall through and halt
            log.write(f"[factory] could not schedule retry: {exc!r}")

    await db.update_run(
        run_id, status="failed", pr_url=pr_url, error=reason, finished_at=db.utcnow()
    )

    if scheduled:
        nxt = attempt + 1
        log.write(f"[factory] attempt {attempt} failed ({reason}); retry {nxt}/{settings.max_attempts} scheduled")
        await _mirror_issue(
            repo,
            number,
            None,
            [],
            log,
            comment=f"Attempt {attempt} failed ({reason}). Retrying — attempt {nxt} of {settings.max_attempts}.",
        )
    else:
        log.write(f"[factory] attempt {attempt} failed ({reason}); no retries left, halting")
        await _mirror_issue(
            repo,
            number,
            github.LABEL_FAILED,
            [github.LABEL_RUNNING, github.LABEL_QUEUED],
            log,
            comment=f"Failed after {attempt} attempt(s) ({reason}). Halting — needs a human.",
        )


async def create(
    repo: str,
    issue_number: int,
    attempt: int = 1,
    prior_error: str | None = None,
    prior_log: str | None = None,
    review_cycle: int = 1,
) -> str:
    """Register a run and schedule it. Returns the run id immediately.

    Which snapshot the run boots is resolved when it actually starts, not now, so a golden
    provisioned while the run sat in the queue is still picked up.

    The `agent` column is seeded with the one agent this deployment runs and then overwritten
    by whatever the golden announces in its manifest on the way in. It is a record of what did
    the work, not an instruction — nothing about dispatch reads it, and no caller chooses it.

    `attempt` > 1 marks a retry: the previous attempt's error and log tail are woven into
    the prompt so the agent diagnoses the failure instead of repeating it. Retries reuse the
    same branch, so the whole chain resolves into one pull request.

    `review_cycle` is the other counter, and it is not the same one. An attempt is a dispatch
    of this build that has to be repeated because it crashed; a cycle is a pass over the pull
    request, opened by a review that sent it back. A fix run is `attempt=1, review_cycle=2` —
    its predecessor did not fail. They are stored in separate columns for that reason.
    """
    issue = await github.get_issue(repo, issue_number)
    run_id = uuid.uuid4().hex
    branch = f"factory/issue-{issue_number}"
    log_path = settings.log_dir / f"{run_id}.log"
    log_path.touch()

    # Which commit this work starts from, recorded now rather than derived later. Only build
    # runs resolve it: a review judges the branch a build pushed and a CI row has no checkout
    # at all, so both belong to the build's base by way of the attempt they share, and giving
    # them their own would be a second answer to a question already answered. Two API calls
    # against a run that lasts twenty minutes.
    base_sha = await github.ref_sha(repo, await github.default_branch(repo))

    await db.create_run(
        id=run_id,
        repo=repo,
        issue_number=issue_number,
        issue_title=issue["title"],
        branch=branch,
        status="queued",
        attempt=attempt,
        cycle=review_cycle,
        agent=agents.DEFAULT_AGENT,
        base_sha=base_sha,
        log_path=str(log_path),
        created_at=db.utcnow(),
    )

    # A proposal the factory has started building is no longer merely proposed. Only on the
    # first attempt of the first cycle: a retry is the same proposal still being built, and
    # `building` → `building` is not an edge the ledger defines.
    if attempt == 1 and review_cycle == 1:
        await advance_improvement(repo, issue_number, "building")

    task = asyncio.create_task(
        _guarded(
            run_id, repo, issue, branch,
            attempt, prior_error, prior_log, review_cycle,
        )
    )
    _tasks[run_id] = task
    task.add_done_callback(lambda _t: _tasks.pop(run_id, None))
    return run_id


async def create_review(
    repo: str,
    issue_number: int,
    pr_url: str,
    branch: str,
    cycle: int = 1,
) -> str:
    """Register and schedule a review of the pull request a build run opened."""
    issue = await github.get_issue(repo, issue_number)
    run_id = uuid.uuid4().hex
    log_path = settings.log_dir / f"{run_id}.log"
    log_path.touch()

    await db.create_run(
        id=run_id,
        repo=repo,
        issue_number=issue_number,
        issue_title=issue["title"],
        branch=branch,
        status="queued",
        kind="review",
        # A review is dispatched once per cycle and never retried, so its attempt is always
        # 1. It used to be `cycle`, which is why the two numbers were indistinguishable.
        attempt=1,
        cycle=cycle,
        agent=agents.DEFAULT_AGENT,
        pr_url=pr_url,
        log_path=str(log_path),
        created_at=db.utcnow(),
    )

    task = asyncio.create_task(
        _guarded_review(run_id, repo, issue, branch, pr_url, cycle)
    )
    _tasks[run_id] = task
    task.add_done_callback(lambda _t: _tasks.pop(run_id, None))
    return run_id


async def create_learn(repo: str) -> str:
    """Register and schedule a learning run for `repo`.

    Carries `issue_number=0` like a provisioning run does, and for the same reason: it is work
    about a repo rather than work on an issue. `db.UNCLAIMED_KINDS` keeps it from blocking that
    repo's queue while it reads.
    """
    run_id = uuid.uuid4().hex
    log_path = settings.log_dir / f"{run_id}.log"
    log_path.touch()

    await db.create_run(
        id=run_id,
        repo=repo,
        issue_number=0,
        issue_title=f"learning from the last {settings.learn_window_days} days",
        status="queued",
        kind="learn",
        attempt=1,
        cycle=1,
        agent=agents.DEFAULT_AGENT,
        log_path=str(log_path),
        created_at=db.utcnow(),
    )

    task = asyncio.create_task(_guarded_learn(run_id, repo))
    _tasks[run_id] = task
    task.add_done_callback(lambda _t: _tasks.pop(run_id, None))
    return run_id


async def _guarded_learn(run_id: str, repo: str) -> None:
    """Run the analyst and own its terminal state, whatever happens.

    A learning run has no issue to label and nothing downstream waiting on it, so unlike a
    build its failure is nobody else's problem — which is exactly why it must still be marked
    terminal here. A learning run stuck in `running` is a row the reconciler will chase a VM
    for, and a repo whose next learning run never fires because the last one never finished.
    """
    log = RunLog(Path(settings.log_dir / f"{run_id}.log"))
    try:
        async with semaphore():
            await _execute_learn(run_id, repo, log)
        await db.update_run(run_id, status="succeeded", finished_at=db.utcnow())
    except asyncio.CancelledError:
        await db.update_run(run_id, status="cancelled", finished_at=db.utcnow())
        raise
    except Exception as exc:  # noqa: BLE001 - the loop failing must not take the factory with it
        log.write(f"[factory] learning run failed: {exc!r}")
        await db.update_run(
            run_id, status="failed", error=f"learn: {exc}", finished_at=db.utcnow()
        )
    finally:
        # Backstop the ledger entry the same way every other agent run does: a run that
        # timed out or crashed still spent what it spent, and the per-call rows are already
        # written, so the cost is recoverable even when the run never reported it.
        await _salvage_usage(run_id, log)
        # `RunLog` holds an open file handle. Every other guard in this system closes its
        # own; this one did not, which is one descriptor per learning run held until the
        # process restarts.
        log.close()


async def _execute_learn(run_id: str, repo: str, log: RunLog) -> None:
    """Read the window, ask an agent what to change, and file what it proposes."""
    base = await github.default_branch(repo)
    window = settings.learn_window_days
    evidence = await digest.build(repo=repo, days=window)
    ledger = await db.list_improvements(repo=repo)

    # Which runs a proposal may cite: this repo's, over the same window the digest covers, so
    # the check is against what the agent was actually shown rather than against all history.
    known_runs = await db.run_ids_since(repo, evidence["window"]["since"])

    boxd = client()
    machine = None
    try:
        await db.update_run(run_id, status="forking", started_at=db.utcnow())
        vm_name = f"{LEARN_PREFIX}{run_id[:8]}"
        log.write(f"[factory] learning from {window} days of {repo}")
        await headroom(boxd, log)
        source = await source_for(boxd, repo, log)
        await db.update_run(run_id, golden=source)
        log.write(f"[factory] provisioning {vm_name} from {source}")
        machine = await _provision(boxd, source, vm_name)
        await db.update_run(run_id, vm_name=machine.name, vm_id=machine.id)
        await boxd.machines.wait_until_ready(machine.id, timeout=180)
        log.write(f"[factory] {machine.name} ready ({machine.id})")

        await db.update_run(run_id, status="running")
        prompt = LEARN_PROMPT_TEMPLATE.format(
            repo=repo,
            base=base,
            days=window,
            digest_path=DIGEST_PATH,
            ledger_path=LEDGER_PATH,
            proposals_path=PROPOSALS_PATH,
            max_proposals=settings.learn_max_proposals,
            metrics=", ".join(LEARN_METRICS),
        )
        env = dispatch_env(
            repo=repo,
            branch=base,
            base=base,
            prompt=prompt,
            run_id=run_id,
            number=0,
            vm_name=machine.name,
            kind="learn",
        )
        env[DIGEST_ENV] = json.dumps(evidence)
        env[LEDGER_ENV] = json.dumps(ledger, default=str)

        exit_code, usage, manifest = await asyncio.wait_for(
            _stream(boxd, machine.id, env, log, run_id, script=LEARN_SCRIPT),
            timeout=settings.run_timeout,
        )
        log.write(f"[factory] analyst exited {exit_code}")
        payload = await _read_json_file(boxd, machine.id, PROPOSALS_PATH, log)
        await _salvage_transcript(boxd, machine.id, run_id, log, manifest)
        # A learning run spends real tokens on a real VM, so it owes the ledger the same
        # entry every other agent run makes. Discarding this was how `learn` became the one
        # kind whose spend was invisible — precisely the gap the trace layer was built to
        # close, reopened by the loop that reads it.
        await db.update_run(
            run_id,
            exit_code=exit_code,
            tokens_in=usage.get("tokens_in"),
            tokens_out=usage.get("tokens_out"),
            cost_usd=usage.get("cost_usd"),
        )
    finally:
        await reap(boxd, machine, log, keep=settings.keep_failed)
        await boxd.close()

    await _grade(payload, ledger, log)
    await _file_proposals(run_id, repo, payload, known_runs, log)


async def _grade(payload: dict | None, ledger: list[dict], log: RunLog) -> None:
    """Record the analyst's verdict on changes that already merged.

    Only rows the ledger actually holds as `merged` can be graded, and only once —
    `db.transition_improvement` enforces both. An id the agent invented, or one it graded a
    second time, raises there and is logged rather than allowed to move anything.
    """
    if not isinstance(payload, dict):
        return
    gradeable = {r["id"] for r in ledger if r.get("status") == "merged"}
    for item in payload.get("gradings") or []:
        if not isinstance(item, dict):
            continue
        row_id = str(item.get("id") or "")
        verdict = str(item.get("verdict") or "")
        if row_id not in gradeable or verdict not in ("kept", "reverted"):
            continue
        observed = item.get("observed")
        try:
            await db.transition_improvement(
                row_id, verdict,
                observed=float(observed) if isinstance(observed, (int, float)) else None,
            )
            log.write(f"[factory] graded {row_id}: {verdict} — {item.get('why') or ''}")
        except ValueError as exc:
            log.write(f"[factory] could not grade {row_id}: {exc}")


async def _file_proposals(
    run_id: str, repo: str, payload: dict | None, known_runs: set[str], log: RunLog
) -> None:
    """Turn validated proposals into issues and ledger rows.

    Two fences live here rather than in the prompt, because a fence in a prompt is a request:

    - **Where.** Every issue is opened on the repo the learning run was dispatched for. The
      agent does not choose a destination, so it cannot propose a change to somebody else's
      repository, to the skills every repo shares, or to this control plane.
    - **Whether it builds.** `agent:queued` is what the poller acts on, and it is added here
      or not at all. `FACTORY_LEARN_AUTOQUEUE` off means the loop still runs, still reasons,
      and stops one step short of changing anything.

    `db.IMPROVEMENT_UNBUILDABLE` proposals never carry the label whatever that setting says.
    A `harness` change is to this control plane and a `compose` change is to the skill that
    writes work orders; neither lives in the repository being learned about, so queuing one
    would point the factory at itself or at its own instructions.
    """
    proposals = valid_proposals(payload, known_runs, settings.learn_max_proposals)
    if not proposals:
        log.write("[factory] no usable proposals — nothing filed")
        return

    for index, proposal in enumerate(proposals):
        buildable = (
            settings.learn_autoqueue
            and proposal["artifact"] not in db.IMPROVEMENT_UNBUILDABLE
        )
        labels = [github.LABEL_QUEUED] if buildable else []
        try:
            issue = await github.create_issue(
                repo, proposal["title"], _proposal_body(proposal, run_id), labels
            )
        except Exception as exc:  # noqa: BLE001 - one failed file must not lose the others
            log.write(f"[factory] could not file {proposal['title']!r}: {exc!r}")
            continue
        try:
            await db.create_improvement(
                id=f"imp_{run_id[:8]}_{index}",
                repo=repo,
                run_id=run_id,
                artifact=proposal["artifact"],
                target=proposal["target"],
                action=proposal["action"],
                rationale=proposal["rationale"],
                evidence=proposal["evidence"],
                metric=proposal["metric"],
                baseline=proposal["baseline"],
                issue_url=issue.get("html_url"),
                issue_number=issue.get("number"),
            )
        except ValueError as exc:
            log.write(f"[factory] filed #{issue.get('number')} but could not record it: {exc}")
            continue
        log.write(
            f"[factory] filed #{issue.get('number')} "
            f"({proposal['artifact']}/{proposal['action']}, "
            f"{'queued' if buildable else 'for review'})"
        )


async def advance_improvement(
    repo: str, issue_number: int, to_status: str, log: RunLog | None = None, **fields
) -> None:
    """Move the proposal behind this issue along, if this issue came from one.

    Best-effort and silent about the ordinary case: nearly every issue the factory builds was
    written by a human and has no ledger row, so "no proposal here" is not worth a line in the
    log. An invalid transition is — it means the ledger and the factory disagree about what
    has happened to something, and that is the one condition under which the grader's input
    stops being trustworthy.

    `log` is optional because one caller has no run log to write to. Dispatch happens before
    the run that owns that file has started, and opening a `RunLog` there to report a
    transition that almost never happens leaked a descriptor on every build the factory
    dispatched. With no run to attribute it to, the control plane's own logger is the honest
    place for it anyway.
    """
    def note(line: str) -> None:
        if log is not None:
            log.write(f"[factory] {line}")
        else:
            _log.info("%s", line)

    try:
        row = await db.improvement_for_issue(repo, issue_number)
        if row is None:
            return
        await db.transition_improvement(row["id"], to_status, **fields)
        note(f"improvement {row['id']} → {to_status}")
    except ValueError as exc:
        note(f"could not advance improvement for #{issue_number}: {exc}")
    except Exception as exc:  # noqa: BLE001 - bookkeeping must never fail a run
        note(f"improvement bookkeeping failed: {exc!r}")


def _proposal_body(proposal: dict, run_id: str) -> str:
    """The issue as a builder reads it, with the reasoning kept above the work.

    The provenance block is not decoration. Six weeks from now the question about any rule in
    a repo is "who decided this and on what", and an issue that cannot answer it produces a
    rule nobody dares delete.
    """
    evidence = json.loads(proposal["evidence"])
    runs = ", ".join(f"`{r}`" for r in evidence.get("run_ids", []))
    return (
        f"{proposal['body']}\n\n"
        f"---\n\n"
        f"*Filed by a learning run ({run_id[:8]}) from telemetry, not by a human.*\n\n"
        f"- **Why:** {proposal['rationale']}\n"
        f"- **Evidence:** {runs}\n"
        f"- **Expected to move:** `{proposal['metric']}` "
        f"(currently {proposal['baseline']})\n"
        f"- **Target:** `{proposal['target'] or 'unspecified'}`\n"
    )


async def _salvage_usage(run_id: str, log: RunLog) -> None:
    """Backstop the run's ledger entry from the telemetry rows.

    A run only reports its own usage in the final `result` event, so a timeout, a crash
    or a cancelled task used to leave `cost_usd = NULL` — real money spent, recorded as
    nothing, on exactly the runs worth understanding. The per-call rows were written as
    the run went, so the spend is still there; this reads it back and fills the gap.

    Only ever fills a gap: a run that reported its own numbers keeps them, so the
    runtime's figure stays authoritative wherever it exists and the derived one is
    strictly a fallback. Best effort by design — never raises.
    """
    try:
        run = await db.get_run(run_id)
        if not run or run.get("cost_usd") is not None:
            return
        derived = await Recorder(run_id).totals()
        if not derived:
            return
        await db.update_run(run_id, **derived)
        log.write(
            f"[factory] usage recovered from telemetry: "
            f"${derived.get('cost_usd') or 0:.2f}, "
            f"{derived.get('tokens_out') or 0} output tokens"
        )
    except asyncio.CancelledError:
        # Called from a `finally` that may already be unwinding a cancelled run. The
        # caller has re-raised, so cancellation still propagates; give up on the number
        # rather than block the teardown.
        return
    except Exception as exc:  # noqa: BLE001 - a missing number must not mask the failure
        log.write(f"[factory] usage salvage skipped: {exc!r}")


async def _guarded_review(
    run_id: str,
    repo: str,
    issue: dict,
    branch: str,
    pr_url: str,
    cycle: int,
) -> None:
    log = RunLog(Path(settings.log_dir / f"{run_id}.log"))
    try:
        async with semaphore():
            await _execute_review(run_id, repo, issue, branch, pr_url, log, cycle)
        # Terminal only now, and this ordering is load-bearing. `_execute_review` used to write
        # `status="succeeded"` the moment the verdict was read, then go on to merge the pull
        # request (up to FACTORY_MERGE_CHECK_TIMEOUT) or create a fix run. For the whole of
        # that window the repo had no non-terminal run, so `db.has_active_run` said idle and
        # the poller claimed the next issue — the one thing `poller._poll_repo` exists to
        # prevent. On 2026-08-21 that dispatched foundation-e-learning #71's fix run and #72's
        # first build in the same second, both branched from a main containing neither.
        #
        # The build path has always got this right and says why at `_fail_or_retry`: the retry
        # is created before the run it replaces is marked terminal. This is the same rule.
        #
        # Holding the run open is safe against the reaper: `reconcile` skips any run still in
        # `_tasks`, and this task is one until `_guarded_review` returns.
        await db.update_run(run_id, status="succeeded", finished_at=db.utcnow())
    except asyncio.CancelledError:
        log.write("[factory] review cancelled")
        await db.update_run(run_id, status="cancelled", finished_at=db.utcnow())
        raise
    except Exception as exc:  # noqa: BLE001
        # A review that crashes must not merge anything, and must not silently strand the PR
        # either. Leave it open, labelled, for a human.
        log.write(f"[factory] review failed: {exc!r}")
        reason = (
            f"timed out after {settings.run_timeout}s"
            if isinstance(exc, asyncio.TimeoutError)
            else f"crashed: {str(exc)[:200] or type(exc).__name__}"
        )
        await db.update_run(
            run_id, status="failed", error=reason, finished_at=db.utcnow()
        )
        await _mirror_issue(
            repo,
            issue["number"],
            github.LABEL_BLOCKED,
            [github.LABEL_RUNNING],
            log,
            comment=f"Review run {reason}. {pr_url} is open and unreviewed — needs a human.",
        )
    finally:
        await _salvage_usage(run_id, log)
        log.close()


async def _guarded(
    run_id: str,
    repo: str,
    issue: dict,
    branch: str,
    attempt: int,
    prior_error: str | None,
    prior_log: str | None,
    review_cycle: int = 1,
) -> None:
    log = RunLog(Path(settings.log_dir / f"{run_id}.log"))
    try:
        async with semaphore():
            await _execute(
                run_id, repo, issue, branch, log,
                attempt, prior_error, prior_log, review_cycle,
            )
    except asyncio.CancelledError:
        # A human stopped this run. Do not retry — cancellation is a decision, not a failure.
        log.write("[factory] run cancelled")
        await db.update_run(run_id, status="cancelled", finished_at=db.utcnow())
        raise
    except Exception as exc:  # noqa: BLE001 - the UI is where failures get reported
        log.write(f"[factory] run failed: {exc!r}")
        # str() on an asyncio.TimeoutError is empty, which used to record the least useful
        # error in the table ("crashed: ") for the one failure mode that says exactly what
        # happened. Name it, and fall back to the exception class for anything else silent.
        if isinstance(exc, asyncio.TimeoutError):
            reason = f"timed out after {settings.run_timeout}s"
        elif isinstance(exc, BudgetExceeded):
            reason = f"over budget: {exc}"
        else:
            reason = f"crashed: {str(exc)[:200] or type(exc).__name__}"
        await _fail_run(
            run_id, repo, issue, attempt, reason, log, review_cycle=review_cycle,
            # The one failure that must not buy another go at the same issue.
            retryable=not isinstance(exc, BudgetExceeded),
        )
    finally:
        await _salvage_usage(run_id, log)
        log.close()


async def _execute(
    run_id: str,
    repo: str,
    issue: dict,
    branch: str,
    log: RunLog,
    attempt: int = 1,
    prior_error: str | None = None,
    prior_log: str | None = None,
    review_cycle: int = 1,
) -> None:
    boxd = client()
    machine = None
    reaped = False
    collected = False
    number = issue["number"]
    try:
        base = await github.default_branch(repo)
        notes = await project_notes(repo, base)
        prompt = build_prompt(
            repo, issue, branch, base, notes, attempt, prior_error, prior_log, review_cycle
        )

        # ---- claim: mirror the pickup onto the issue for anyone watching on GitHub
        #
        # Each counter against the budget that actually caps it. This used to be one line
        # reading "attempt {attempt} of {max_attempts}" for both, so a fix run opened by a
        # review said "attempt 2 of 3" when the budget governing it was `max_review_cycles`
        # — 2 — and it was already on the last one. Nobody watching the issue could tell the
        # run was the factory's final unsupervised go.
        parts = []
        if review_cycle > 1:
            parts.append(f"fix cycle {review_cycle} of {settings.max_review_cycles}")
        if attempt > 1:
            parts.append(f"attempt {attempt} of {settings.max_attempts}")
        which = f" ({', '.join(parts)})" if parts else ""
        started = f"Factory run started on branch `{branch}`{which}."
        link = _run_link(run_id)
        if link:
            started += f"\n\nLive log: {link}"
        # Clear every label that means "stopped", not just `agent:queued`. A run dispatched by
        # hand onto a blocked or failed issue used to leave that label in place, and the poller
        # halts a repo while any open issue carries one — so an issue that was resumed and fixed
        # went on halting every issue behind it unless it happened to close. Whatever the run
        # goes on to do will set the right label at the end; while it is running, none of these
        # is true.
        await _mirror_issue(
            repo,
            number,
            github.LABEL_RUNNING,
            [github.LABEL_QUEUED, github.LABEL_BLOCKED, github.LABEL_FAILED],
            log,
            comment=started,
        )

        # ---- provision
        await db.update_run(run_id, status="forking", started_at=db.utcnow())
        vm_name = f"{RUN_PREFIX}{run_id[:8]}"
        await headroom(boxd, log)
        source = await source_for(boxd, repo, log)
        await db.update_run(run_id, golden=source)
        log.write(f"[factory] provisioning {vm_name} from {source}")
        machine = await _provision(boxd, source, vm_name)
        await db.update_run(run_id, vm_name=machine.name, vm_id=machine.id)
        await boxd.machines.wait_until_ready(machine.id, timeout=180)
        log.write(f"[factory] {machine.name} ready ({machine.id})")

        # ---- run the agent
        await db.update_run(run_id, status="running")
        env = dispatch_env(
            repo=repo,
            branch=branch,
            base=base,
            prompt=prompt,
            run_id=run_id,
            number=number,
            vm_name=machine.name,
            # Either counter being past its first value means the branch already carries
            # commits: a crashed attempt pushed some, or a review sent an approved pull
            # request back for another pass.
            resume=attempt > 1 or review_cycle > 1,
        )
        exit_code, usage, manifest = await asyncio.wait_for(
            _stream(boxd, machine.id, env, log, run_id), timeout=settings.run_timeout
        )
        log.write(f"[factory] agent exited {exit_code}")
        await db.update_run(
            run_id,
            exit_code=exit_code,
            tokens_in=usage.get("tokens_in"),
            tokens_out=usage.get("tokens_out"),
            cost_usd=usage.get("cost_usd"),
        )

        # ---- collect
        pr_url = await github.find_pr(repo, branch)
        if pr_url:
            log.write(f"[factory] pull request: {pr_url}")
        else:
            log.write("[factory] no pull request found for this branch")
        await _salvage_transcript(boxd, machine.id, run_id, log, manifest)
        # Whatever the agent proposed, read out while the VM still exists. This is the
        # ordinary path and it covers a failed agent as well as a successful one — a run that
        # exited non-zero still gets here, and is often the one that learned the most.
        await _collect_memory_candidates(boxd, machine.id, run_id, repo, log)
        collected = True

        ok = exit_code == 0 and pr_url is not None
        review_next = False
        if ok:
            criteria = parse_criteria(issue.get("body") or "")
            review_next = settings.review_enabled and bool(criteria)
            merged = False
            if review_next:
                # Hand the PR to a reviewing agent instead of merging on the builder's word.
                # An issue with no machine-readable criteria has nothing to review against, so
                # it falls through to the old path rather than being reviewed against a guess.
                log.write(f"[factory] {len(criteria)} acceptance criteria; queueing review")
            elif settings.auto_merge:
                # Merge now so the next issue in the sequence branches from a main that
                # already contains this issue's work.
                if not criteria:
                    log.write("[factory] issue carries no acceptance criteria; skipping review")
                merged = (
                    await _merge(
                        repo, pr_url, base, log,
                        issue=issue, branch=branch, cycle=review_cycle,
                    )
                ).merged
            await db.update_run(
                run_id, status="succeeded", pr_url=pr_url, error=None, finished_at=db.utcnow()
            )
            outcome = f"Factory run finished. Pull request: {pr_url}"
            if merged:
                outcome += " (auto-merged)"
            elif review_next:
                outcome += " — under review"
            await _mirror_issue(
                repo,
                number,
                None if review_next else github.LABEL_DONE,
                [] if review_next else [github.LABEL_RUNNING],
                log,
                comment=outcome,
            )
        else:
            reason = f"exit {exit_code}, {'no ' if not pr_url else ''}pull request"
            await _fail_run(
                run_id, repo, issue, attempt, reason, log,
                pr_url=pr_url, review_cycle=review_cycle,
            )

        # ---- reap
        keep = not ok and settings.keep_failed
        if not keep:
            # Brief drain so any buffered telemetry flushes before the VM disappears.
            await asyncio.sleep(3)
        await reap(boxd, machine, log, keep=keep)
        reaped = True

        # ---- hand off to review, once this run's VM is gone and its slot is free
        if ok and review_next:
            try:
                await create_review(repo, number, pr_url, branch, cycle=review_cycle)
            except Exception as exc:  # noqa: BLE001 - an unreviewed PR beats a lost one
                log.write(f"[factory] could not queue review: {exc!r}")
    finally:
        # Everything above can raise: `wait_until_ready` times out, the stream drops, the run
        # hits `FACTORY_RUN_TIMEOUT`, a GitHub call fails after the agent finished. Each of
        # those used to leave a machine running until its two-hour self-destruct. The reap in
        # the body is the ordinary path and this is the one that catches the rest; `reaped`
        # keeps it from running twice.
        # The exceptional path: the stream dropped, the run timed out, a GitHub call blew up
        # after the agent had finished. The ordinary collection above never ran, and the reap
        # below is about to take the file with it — so this is the last moment there is.
        if machine is not None and not collected:
            await _collect_memory_candidates(boxd, machine.id, run_id, repo, log)
        if not reaped:
            await reap(boxd, machine, log, keep=settings.keep_failed)
        await boxd.close()


async def _execute_review(
    run_id: str,
    repo: str,
    issue: dict,
    branch: str,
    pr_url: str,
    log: RunLog,
    cycle: int,
) -> None:
    """Fork a VM, review the PR against the issue's criteria, then merge it or send it back."""
    number = issue["number"]
    base = await github.default_branch(repo)
    criteria = parse_criteria(issue.get("body") or "")
    boxd = client()
    machine = None
    reaped = False
    try:
        await db.update_run(run_id, status="forking", started_at=db.utcnow())
        vm_name = f"{REVIEW_PREFIX}{run_id[:8]}"
        log.write(f"[factory] review {cycle}/{settings.max_review_cycles} of {pr_url}")
        await headroom(boxd, log)
        source = await source_for(boxd, repo, log)
        await db.update_run(run_id, golden=source)
        log.write(f"[factory] provisioning {vm_name} from {source}")
        machine = await _provision(boxd, source, vm_name)
        await db.update_run(run_id, vm_name=machine.name, vm_id=machine.id)
        await boxd.machines.wait_until_ready(machine.id, timeout=180)
        log.write(f"[factory] {machine.name} ready ({machine.id})")

        pr_number = int(pr_url.rstrip("/").split("/")[-1])
        head_sha = await github.pr_head_sha(repo, pr_number)
        base_sha = await github.merge_base_sha(repo, base, head_sha)

        await db.update_run(run_id, status="running")
        prompt = REVIEW_PROMPT_TEMPLATE.format(
            project_notes=await project_notes(repo, base),
            repo=repo,
            number=number,
            title=issue["title"],
            pr_url=pr_url,
            branch=branch,
            base=base,
            base_sha=base_sha,
            criteria=yaml.safe_dump(criteria, sort_keys=False, allow_unicode=True),
            body=issue.get("body") or "(no description given)",
        )
        env = dispatch_env(
            repo=repo,
            branch=branch,
            base=base,
            prompt=prompt,
            run_id=run_id,
            number=number,
            vm_name=machine.name,
            kind="review",
        )

        exit_code, usage, manifest = await asyncio.wait_for(
            _stream(boxd, machine.id, env, log, run_id, script=REVIEW_SCRIPT),
            timeout=settings.run_timeout,
        )
        log.write(f"[factory] reviewer exited {exit_code}")
        verdict = await _read_verdict(boxd, machine.id, log)
        await _salvage_transcript(boxd, machine.id, run_id, log, manifest)

        approved, why, findings = decide(verdict, criteria)
        # Everything the reviewer produced, recorded now — but deliberately *not* `status` or
        # `finished_at`. A review is not finished when the reviewer stops talking; it is
        # finished when what it decided has been scheduled, which is what the rest of this
        # function does. `_guarded_review` marks it terminal on the way out. See the note there.
        await db.update_run(
            run_id,
            exit_code=exit_code,
            tokens_in=usage.get("tokens_in"),
            tokens_out=usage.get("tokens_out"),
            cost_usd=usage.get("cost_usd"),
            verdict=json.dumps(verdict) if verdict else None,
            error=None if approved else f"{REVIEW_REFUSED}{why}",
        )
        log.write(f"[factory] verdict: {'approve' if approved else 'request changes'} — {why}")
        for finding in findings:
            log.write(f"[factory]   finding: {finding}")

        await asyncio.sleep(3)
        await reap(boxd, machine, log)
        reaped = True

        # ---- act on the verdict
        #
        # An approved pull request has three possible endings, and they are not the same
        # thing. Merged is finished. Red CI is a real defect in code a reviewer signed off,
        # which is exactly what a fix run is for. Anything else — checks still pending,
        # GitHub unreachable, the merge itself refused — means we do not know what is wrong,
        # so it stops for a human. Collapsing all three into `agent:done` is how a broken
        # pull request ends up sitting open under an issue that claims to be finished.
        if approved:
            merge_attempt = MergeAttempt(False, None, "auto-merge is off")
            if settings.auto_merge:
                merge_attempt = await _merge(
                    repo, pr_url, base, log, issue=issue, branch=branch, cycle=cycle
                )
            outcome = merge_outcome(
                settings.auto_merge, merge_attempt.merged, merge_attempt.ci_failure
            )

            if outcome == "done":
                comment = f"Review passed — {why}. {pr_url}"
                if merge_attempt.merged:
                    comment += " (merged)"
                await _mirror_issue(
                    repo, number, github.LABEL_DONE, [github.LABEL_RUNNING], log, comment=comment
                )
                return

            if outcome == "human":
                await db.update_run(run_id, error=f"{REVIEW_UNMERGED}{merge_attempt.why}")
                await _mirror_issue(
                    repo,
                    number,
                    github.LABEL_BLOCKED,
                    [github.LABEL_RUNNING, github.LABEL_QUEUED],
                    log,
                    comment=(
                        f"Review passed — {why} — but the pull request could not be merged: "
                        f"{merge_attempt.why}. Needs a human. {pr_url}"
                    ),
                )
                return

            # Hand the failing job's log to the fix run. Without it the next agent is told
            # only that a check named `gates` failed, which is equally true of a broken test
            # and of a registry timeout.
            # Tagged with which of the three things went wrong — see REVIEW_CI_RED.
            await db.update_run(run_id, error=f"{REVIEW_CI_RED}{merge_attempt.ci_failure}")
            # Already fetched, on the commit the checks actually judged, while the CI phase
            # was being recorded. Fetching it again here would ask about whatever the head is
            # by now — a different question with a plausible-looking answer.
            ci_log = merge_attempt.ci_log or "(no failing check log could be read)"
            await _fix_cycle(
                repo,
                number,
                cycle,
                log,
                reason=f"CI failed after an approved review: {merge_attempt.ci_failure}",
                detail=(
                    f"CI failed on the pull request after the review approved it.\n"
                    f"{merge_attempt.ci_failure}\n\n"
                    "The reviewer confirmed every acceptance criterion against real command "
                    "output, so the change itself is sound. What failed is something CI runs "
                    "that this VM does not.\n\n"
                    "The failing job's log is below — read it before you change anything, and "
                    "decide first which kind of failure it is.\n\n"
                    "If it is an infrastructure fault rather than a defect in this change — an "
                    "image pull or network timeout, a runner that died, a rate limit, a service "
                    "that was briefly unavailable — then the code is fine and editing it would "
                    "make things worse. Re-run the job instead and wait for the result:\n"
                    f"  gh run list --branch {branch} --limit 1\n"
                    "  gh run rerun <run-id> --failed\n\n"
                    "If it is a real defect, fix its cause. Do not delete, skip or weaken a "
                    "test to make it pass, and do not edit CI configuration — a gate that "
                    "fails is doing its job.\n\n"
                    f"--- failing check log ---\n{ci_log}"
                ),
                going_back=(
                    f"Review passed, but CI is red ({merge_attempt.ci_failure}). "
                    f"Fixing. {pr_url}"
                ),
                giving_up=(
                    f"Review passed, but CI is still red after {cycle} cycles "
                    f"({merge_attempt.ci_failure}).\n\n"
                    f"Stopping — needs a human. {pr_url}"
                ),
            )
            return

        detail = "\n".join(f"- {f}" for f in findings) or "- (no specific findings recorded)"
        await _fix_cycle(
            repo,
            number,
            cycle,
            log,
            reason=f"review requested changes: {why}",
            detail=f"Reviewer findings:\n{detail}",
            going_back=f"Review {cycle} requested changes ({why}):\n{detail}\n\nFixing.",
            giving_up=(
                f"Review still requesting changes after {cycle} cycles ({why}):\n{detail}"
                f"\n\nStopping — the issue may be wrong rather than the code. {pr_url}"
            ),
        )
    finally:
        # The review path leaks a VM the same way the build path does, and for the same
        # reasons — see the note there.
        if not reaped:
            await reap(boxd, machine, log, keep=settings.keep_failed)
        await boxd.close()


async def _read_json_file(
    boxd: AsyncBoxd, machine_id: str, path: str, log: RunLog
) -> dict | None:
    """Read one JSON file out of the VM before it is reaped. None on any failure.

    Every caller treats None as "the agent produced nothing usable", and every caller is
    right to: a reviewer that wrote no verdict has approved nothing, an analyst that wrote
    no proposals has proposed nothing, and a planner that wrote no verdict has planned
    nothing. Failing closed is the same answer in all cases, which is why they share this.
    """
    try:
        result = await boxd.machines.exec(machine_id, f"cat {path}", timeout=30)
        raw = getattr(result, "stdout", None) or ""
        if inspect.isawaitable(raw):  # pragma: no cover - SDK shape drift
            raw = await raw
        text = raw.strip()
        if not text:
            log.write(f"[factory] nothing written to {path}")
            return None
        # The agent sometimes wraps JSON in a ``` fence despite being asked not to.
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\n|\n```$", "", text.strip())
        return json.loads(text)
    except json.JSONDecodeError as exc:
        log.write(f"[factory] {path} is not valid JSON: {exc}")
        return None
    except Exception as exc:  # noqa: BLE001
        log.write(f"[factory] could not read {path}: {exc!r}")
        return None


async def _read_verdict(boxd: AsyncBoxd, machine_id: str, log: RunLog) -> dict | None:
    """The reviewer's verdict, or None — which `decide` treats as "not approved"."""
    return await _read_json_file(boxd, machine_id, VERDICT_PATH, log)


async def _stream(
    boxd: AsyncBoxd,
    machine_id: str,
    env: dict,
    log: RunLog,
    run_id: str,
    script: str = VM_SCRIPT,
) -> tuple[int, dict, dict]:
    """Run the agent, formatting its event stream into the log as it arrives.

    Returns the exit code, the usage captured from the final `result` event
    (input/output tokens and cost), which is what the Runs UI shows as the run's cost,
    and the manifest the golden announced before handing off — `{}` when it announced
    none, which is every golden captured before the wrapper existed.

    The same events also go to the telemetry recorder, which normalizes them into
    per-call rows and writes them as the run proceeds. That is a second consumer of a
    stream we were already parsing, not a second stream: the facts telemetry wants are
    the ones scrolling past here, and the only thing that used to happen to them was
    being formatted into a log line and forgotten.
    """
    buffer = ""
    usage: dict = {}
    manifest: dict = {}
    receipt: dict | None = None
    recorder = Recorder(run_id)
    async with boxd.machines.stream_exec(
        machine_id, command=export_prelude(env) + script, env=env, close_stdin=True
    ) as stream:
        async for chunk in stream.iter_chunks():
            text = chunk.data.decode("utf-8", errors="replace")
            if chunk.is_stderr:
                for line in text.splitlines():
                    if line.strip():
                        log.write(f"[stderr] {line}")
                continue
            buffer += text
            while "\n" in buffer:
                line, _, buffer = buffer.partition("\n")
                line = line.strip()
                if not line:
                    continue
                if line.startswith(MANIFEST_PREFIX):
                    # The golden speaking about itself, not the agent speaking. It is
                    # handled before the JSON branch and consumed here: the telemetry
                    # recorder normalizes agent events and would count this as a dropped
                    # one, and logging it raw would put a line of machine handshake in
                    # the middle of a log a human reads.
                    manifest = parse_manifest(line)
                    named = str(manifest.get("agent") or "").strip()
                    # Before a single event is read, which is the only moment an
                    # adapter can still be chosen.
                    events = recorder.use(manifest.get("events"))
                    if manifest:
                        log.write(
                            f"[factory] manifest: agent {named or 'unnamed'}, "
                            f"events {events}, transcript {transcript_glob(manifest)}"
                        )
                    else:
                        log.write("[factory] agent manifest unreadable; using defaults")
                    await db.update_run(
                        run_id,
                        manifest=json.dumps(manifest),
                        # What actually ran, overwriting the default the row was seeded
                        # with. This is the registry: the image says which agent it launches,
                        # and nothing else in the system claims to know.
                        **({"agent": named} if named else {}),
                    )
                    continue
                if line.startswith("{"):
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        log.write(line)
                        continue
                    # Asked of every event, because which one carries the run's
                    # figures is the adapter's business. A null adapter answers `{}`
                    # to all of them, and the run records no cost rather than a wrong
                    # one — `_salvage_usage` fills the ledger from rows where there
                    # are any.
                    usage = recorder.summary(event) or usage
                    if await recorder.feed(event) and settings.max_run_cost:
                        # A turn just closed, so the derived cost has moved and this is the
                        # cheapest moment it can be read. The wall clock was never the right
                        # ceiling — a run can burn the budget in twenty minutes or idle for
                        # ninety — and the number this compares against is the same one every
                        # report uses, not an estimate made here.
                        spent = await recorder.cost()
                        if spent > settings.max_run_cost:
                            log.write(
                                f"[factory] run stopped at ${spent:.2f}, over the "
                                f"${settings.max_run_cost:.2f} ceiling"
                            )
                            raise BudgetExceeded(
                                f"stopped at ${spent:.2f}, over the "
                                f"${settings.max_run_cost:.2f} per-run ceiling"
                            )
                    for formatted in stream_lines(event, line):
                        log.write(formatted)
                    better = _better_receipt(receipt, _receipt_candidates(event))
                    if better is not None:
                        receipt = better
                        await _persist_memory_receipt(run_id, receipt, log)
                    continue
                better = _better_receipt(receipt, (line,))
                if better is not None:
                    receipt = better
                    await _persist_memory_receipt(run_id, receipt, log)
                log.write(line)
        code = stream.exit_code
        if inspect.isawaitable(code):
            code = await code
    await recorder.close()
    if recorder.dropped:
        log.write(f"[factory] telemetry dropped {recorder.dropped} events")
    return int(code or 0), usage, manifest


async def _salvage_transcript(
    boxd: AsyncBoxd, machine_id: str, run_id: str, log: RunLog, manifest: dict | None = None
) -> None:
    """Copy the agent's session transcript out before the VM is destroyed.

    The live stream is for watching; this file is the complete, replayable record. Best
    effort by design — a missing transcript must never fail an otherwise good run.

    Where to look comes from the manifest, because the control plane no longer knows what
    is running inside the VM. Without that, this reaches for `~/.claude` on every machine
    and quietly salvages nothing from any agent that is not Claude Code.
    """
    script = (
        f'f=$(ls -t {transcript_glob(manifest)} 2>/dev/null | head -1); '
        '[ -n "$f" ] && [ "$(wc -c < "$f")" -lt 20000000 ] && cat "$f" || true'
    )
    try:
        result = await boxd.machines.exec(machine_id, script, timeout=120)
        if result.stdout.strip():
            path = settings.log_dir / f"{run_id}.transcript.jsonl"
            path.write_text(result.stdout, encoding="utf-8")
            await db.update_run(run_id, transcript_path=str(path))
            log.write(f"[factory] transcript saved ({len(result.stdout)} bytes)")
    except Exception as exc:  # noqa: BLE001
        log.write(f"[factory] transcript salvage skipped: {exc!r}")


def track(run_id: str, task: asyncio.Task) -> None:
    """Register a run's task so `cancel()` can reach it, and forget it when it ends.

    Public because provisioning a golden is a run this module does not start — it is an
    agentless one, so it lives in `control/provision.py` — but it is still a task holding a VM,
    and a task the UI cannot cancel is a VM nobody can stop.
    """
    _tasks[run_id] = task
    task.add_done_callback(lambda _t: _tasks.pop(run_id, None))


async def cancel(run_id: str) -> bool:
    """Cancel an in-flight run and destroy its VM."""
    run = await db.get_run(run_id)
    if not run or run["status"] in db.TERMINAL:
        return False
    task = _tasks.get(run_id)
    if task:
        task.cancel()
    if run.get("vm_id"):
        boxd = client()
        try:
            await boxd.machines.delete(run["vm_id"])
        except Exception:  # noqa: BLE001 - already gone is fine
            pass
        finally:
            await boxd.close()
    await db.update_run(run_id, status="cancelled", finished_at=db.utcnow())
    return True


_reconciler: asyncio.Task | None = None


async def _reconcile_loop() -> None:
    _log.info("reconciling the fleet every %ss", settings.reconcile_interval)
    while True:
        await asyncio.sleep(settings.reconcile_interval)
        try:
            found = await reconcile()
        except Exception:  # noqa: BLE001 - a boxd outage is a sweep that did nothing
            _log.exception("reconcile failed")
            continue
        if found["destroyed"] or found["stranded"] or found["stuck"]:
            _log.info(
                "reconciled: destroyed %s, stranded %s, stuck %s",
                found["destroyed"], found["stranded"], found["stuck"],
            )


def start_reconciler() -> None:
    """Run the sweep on a timer.

    `control/README.md` §4 has described "a periodic reconciler" since the layer was designed.
    Until now the function existed and nothing scheduled it: the only way a leaked VM was ever
    reclaimed was somebody noticing and pressing a button in the fleet view. Sleeps first, so a
    control plane in a restart loop cannot turn into a sweep loop.
    """
    global _reconciler
    if _reconciler is not None or settings.reconcile_interval <= 0:
        return
    _reconciler = asyncio.create_task(_reconcile_loop())


async def stop_reconciler() -> None:
    global _reconciler
    if _reconciler is None:
        return
    _reconciler.cancel()
    try:
        await _reconciler
    except asyncio.CancelledError:
        pass
    _reconciler = None


async def reconcile() -> dict:
    """Compare the boxd fleet against the runs table and resolve the difference.

    Fleet state belongs in the database, not in anybody's head. Without this, a crashed
    dispatch silently leaks machines against the quota.

    Safe to call at any time and from anywhere: it reads both sides fresh, and a run whose task
    is still in flight is never touched (`_tasks`). `headroom` calls it before giving up on a
    full fleet, and `_reconcile_loop` calls it on a timer.
    """
    boxd = client()
    try:
        machines = await boxd.machines.list()
        active = await db.active_runs()
        active_vms = {r["vm_name"] for r in active if r.get("vm_name")}

        orphans = [
            m for m in machines if is_run_vm(m.name) and m.name not in active_vms
        ]
        # Per machine, because one that refuses to die used to abort the whole sweep — and the
        # sweep is what reclaims the quota, so a single stuck VM could keep every other orphan
        # alive behind it. Reported rather than raised: the next sweep tries again.
        destroyed, stuck = [], []
        for machine in orphans:
            try:
                await boxd.machines.delete(machine.id)
                destroyed.append(machine.name)
            except Exception as exc:  # noqa: BLE001 - the message is the finding
                _log.warning("could not destroy orphan %s: %r", machine.name, exc)
                stuck.append(machine.name)

        # Runs we think are live whose VM no longer exists.
        names = {m.name for m in machines}
        stranded = []
        for run in active:
            if run.get("vm_name") and run["vm_name"] not in names and run["id"] not in _tasks:
                await db.update_run(
                    run["id"],
                    status="failed",
                    error="VM disappeared while run was active",
                    finished_at=db.utcnow(),
                )
                stranded.append(run["id"])
        return {"destroyed": destroyed, "stuck": stuck, "stranded": stranded}
    finally:
        await boxd.close()
