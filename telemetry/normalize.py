"""The adapter boundary: one runtime's event stream in, vendor-neutral rows out.

Everything that knows what a Claude Code event looks like lives in this file. Nothing
downstream of it — no table, no query, no page — mentions Claude. That is the whole
point of §2 of this layer's README, and it is the reason adding a second runtime is one
more class here rather than a migration.

**Pure by construction.** No I/O, no clock, no database. Feed it events, get rows back.
That is what lets the same normalizer serve two sources without a second implementation:
the live stream the runner already parses (`recorder.py`) and a salvaged session
transcript replayed after the fact (`backfill.py`). An OTLP ingest, when it arrives,
becomes a third caller rather than a rewrite.

The two sources differ in shape only at the edges — the stream carries
`parent_tool_use_id` on the envelope, the transcript carries `isSidechain`/`parentUuid`
instead — so every field is read defensively and an unrecognised event yields no rows
rather than an exception. The event schema is not a contract; a run must never fail
because telemetry could not parse something.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

# Fields worth keeping off a tool's input, in the order we prefer them. Enough to tell
# two Bash calls apart in a leaderboard; never the whole input, which can be a file.
HINT_KEYS = ("command", "file_path", "pattern", "skill", "url", "query", "description")
HINT_MAX = 200
ERROR_MAX = 500


@dataclass(frozen=True)
class LlmCall:
    """One model call.

    Cache tokens are split by TTL because they are not priced alike: a 5-minute write
    costs 1.25x the input rate and a 1-hour write costs 2x. The agent runtime asks for
    1-hour caching, so collapsing the two would understate the second-largest component
    of a run's cost by roughly 40%.
    """

    run_id: str
    turn: int
    ts: str | None
    model: str | None
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_5m_tokens: int
    cache_write_1h_tokens: int
    parent_call_id: str | None = None


@dataclass(frozen=True)
class ToolCall:
    """One tool invocation, paired with its result.

    `id` is the runtime's own tool-use id, so re-normalizing the same transcript
    overwrites rather than duplicates — which is what makes backfill idempotent.
    """

    id: str
    run_id: str
    turn: int
    ts: str | None
    tool: str
    ok: bool
    duration_ms: int | None
    error: str | None
    detail: str | None
    parent_call_id: str | None = None


def _int(value: object) -> int:
    return value if isinstance(value, int) else 0


def _text(content: object) -> str:
    """Flatten a tool_result's content, which is either a string or a block list."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            b.get("text") or ""
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        return "\n".join(p for p in parts if p)
    return ""


def _hint(supplied: dict) -> str | None:
    """A short, human-readable label for what a tool call was doing."""
    for key in HINT_KEYS:
        value = supplied.get(key)
        if value:
            return str(value).replace("\n", " ")[:HINT_MAX]
    return None


def _blocks(message: dict, kind: str):
    """The message's content blocks of one type, skipping anything malformed."""
    for block in message.get("content") or []:
        if isinstance(block, dict) and block.get("type") == kind:
            yield block


def _cache_writes(usage: dict) -> tuple[int, int]:
    """Cache-write tokens as (5-minute, 1-hour).

    The runtime reports the two TTLs separately when it can. Older payloads carry only
    the combined figure, which is attributed to the 5-minute tier — it is the cheaper of
    the two, so a missing split can never inflate a derived cost.
    """
    split = usage.get("cache_creation") or {}
    if not split:
        return _int(usage.get("cache_creation_input_tokens")), 0
    return (
        _int(split.get("ephemeral_5m_input_tokens")),
        _int(split.get("ephemeral_1h_input_tokens")),
    )


def elapsed_ms(start: str | None, end: str | None) -> int | None:
    """Milliseconds between two ISO timestamps, or None if either is unusable.

    Duration is derived rather than reported: the runtime timestamps the event that
    requests a tool and the event that returns its result, and the gap between them is
    the tool's wall time. That is the number that answers "what is this run waiting on".
    """
    if not start or not end:
        return None
    try:
        delta = dt.datetime.fromisoformat(end) - dt.datetime.fromisoformat(start)
    except (TypeError, ValueError):
        return None
    return int(delta.total_seconds() * 1000)


class ClaudeCodeAdapter:
    """Normalizes one run's worth of Claude Code events.

    Stateful over the stream, because two of the three things we want are only knowable
    across events: the turn index (a counter, since no event carries one) and a tool
    call's duration (the gap between the request and its result). One instance per run.
    """

    name = "claude-code"

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.turn = 0
        # tool_use_id -> the request half, waiting for its result.
        self._pending: dict[str, dict] = {}
        # Message ids whose usage has already been counted. See `_assistant`.
        self._counted: set[str] = set()

    def feed(self, event: dict) -> list[LlmCall | ToolCall]:
        """Rows produced by this event. Empty for events that carry no facts."""
        if not isinstance(event, dict):
            return []
        kind = event.get("type")
        if kind == "assistant":
            return self._assistant(event)
        if kind == "user":
            return self._user(event)
        return []

    def flush(self) -> list[ToolCall]:
        """Tool calls that never got a result — the run ended inside them.

        Emitted as failures rather than dropped, because "the run died in Bash at turn
        41" is exactly the diagnosis the old one-row-per-run ledger could not give.
        """
        rows = [
            ToolCall(
                id=call_id,
                run_id=self.run_id,
                turn=p["turn"],
                ts=p["ts"],
                tool=p["tool"],
                ok=False,
                duration_ms=None,
                error="no result (run ended)",
                detail=p["detail"],
                parent_call_id=p["parent"],
            )
            for call_id, p in self._pending.items()
        ]
        self._pending.clear()
        return rows

    def summary(self, event: dict) -> dict:
        """The run-level figures this runtime reports in its final `result` event.

        `{}` for every other event, which is what lets the caller hand the whole stream
        through here without knowing that Claude Code calls its last event `result` —
        that knowledge was the last piece of one runtime's vocabulary left in `control`.

        Returned as `runs` column names, so the caller can hand it straight to an update.
        Kept exactly as the runtime reports them: they are not the same quantity the rows
        sum to — the runtime bills side calls (title generation and the like) that never
        surface as assistant events — so the two disagreeing is signal, not a bug. The rows
        are what the agent did; this is what the runtime charged for.
        """
        if not isinstance(event, dict) or event.get("type") != "result":
            return {}
        usage = event.get("usage") or {}
        return {
            "tokens_in": usage.get("input_tokens"),
            "tokens_out": usage.get("output_tokens"),
            "cost_usd": event.get("total_cost_usd"),
        }

    # ------------------------------------------------------------------ internals

    def _assistant(self, event: dict) -> list[LlmCall | ToolCall]:
        message = event.get("message") or {}
        ts = event.get("timestamp")
        parent = event.get("parent_tool_use_id")
        rows: list[LlmCall | ToolCall] = []

        usage = message.get("usage") or {}
        if usage and self._first_sighting(message):
            self.turn += 1
            write_5m, write_1h = _cache_writes(usage)
            rows.append(
                LlmCall(
                    run_id=self.run_id,
                    turn=self.turn,
                    ts=ts,
                    model=message.get("model"),
                    input_tokens=_int(usage.get("input_tokens")),
                    output_tokens=_int(usage.get("output_tokens")),
                    cache_read_tokens=_int(usage.get("cache_read_input_tokens")),
                    cache_write_5m_tokens=write_5m,
                    cache_write_1h_tokens=write_1h,
                    parent_call_id=parent,
                )
            )

        # Collected from every entry, including repeats — a split message delivers its
        # tool uses across the same entries whose usage was deduplicated above.
        for block in _blocks(message, "tool_use"):
            call_id = block.get("id")
            if not call_id:
                continue
            self._pending[call_id] = {
                "tool": block.get("name") or "tool",
                "ts": ts,
                "turn": self.turn,
                "detail": _hint(block.get("input") or {}),
                "parent": parent,
            }
        return rows

    def _first_sighting(self, message: dict) -> bool:
        """Whether this entry's usage should be billed, and remember that it was.

        One API response can reach us as several entries: the session transcript writes
        one per content block (thinking, then text, then each tool_use) and repeats the
        *same* `usage` object on every one — observed up to 13 times for one message.
        Billing per entry inflated token totals by ~2x across 37 real runs. Billing per
        `message.id` is the fix. The live stream sends one event per message, so this is
        a no-op there.

        A message with no id cannot be deduplicated, so it is billed: losing a real call
        from the ledger is the worse error.
        """
        message_id = message.get("id")
        if message_id is None:
            return True
        if message_id in self._counted:
            return False
        self._counted.add(message_id)
        return True

    def _user(self, event: dict) -> list[LlmCall | ToolCall]:
        message = event.get("message") or {}
        ts = event.get("timestamp")
        rows: list[LlmCall | ToolCall] = []

        for block in _blocks(message, "tool_result"):
            call_id = block.get("tool_use_id")
            pending = self._pending.pop(call_id, None)
            if pending is None:
                # A result with no matching request: a truncated transcript, or a
                # backfill that started mid-stream. Nothing useful to record.
                continue
            failed = bool(block.get("is_error"))
            error = None
            if failed:
                error = _text(block.get("content")).strip()[:ERROR_MAX] or None
            rows.append(
                ToolCall(
                    id=call_id,
                    run_id=self.run_id,
                    turn=pending["turn"],
                    ts=pending["ts"],
                    tool=pending["tool"],
                    ok=not failed,
                    duration_ms=elapsed_ms(pending["ts"], ts),
                    error=error,
                    detail=pending["detail"],
                    parent_call_id=pending["parent"],
                )
            )
        return rows


class NullAdapter:
    """The adapter for a runtime this layer does not understand yet.

    Every method is the empty answer, so a golden carrying an agent nobody has written an
    adapter for still runs, still streams its log, and still opens its pull request — it
    simply records no per-call rows and no cost. That is the point: telemetry is a
    consumer of a run, never a precondition for one, and a missing adapter must degrade to
    a missing number rather than to a refused dispatch.

    Recording nothing is also the honest answer rather than a lossy one. A run with no
    adapter reports no cost instead of a wrong cost, and `runner._salvage_usage` fills the
    ledger from the rows wherever any exist — which, here, is nowhere.
    """

    name = "null"

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.turn = 0

    def feed(self, event: dict) -> list[LlmCall | ToolCall]:
        return []

    def flush(self) -> list[ToolCall]:
        return []

    def summary(self, event: dict) -> dict:
        return {}


# Every runtime this layer can read, keyed by the `events` string a golden announces in
# its manifest. One entry per adapter, and adding a runtime is adding a class and a line
# here — never a change in `control`, which is what §2 of this layer's README promises.
ADAPTERS = {ClaudeCodeAdapter.name: ClaudeCodeAdapter}


def adapter_for(events: str | None, run_id: str):
    """The adapter for an `events` format, or the null adapter for anything unknown.

    Deliberately total. `events` arrives from a file on a machine we did not build, so
    every failure to recognise it — a typo, an agent added before its adapter, a manifest
    that names none at all — resolves to an adapter rather than to an exception.
    """
    return ADAPTERS.get((events or "").strip(), NullAdapter)(run_id)
