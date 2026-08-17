"""The normalizer is the only place in this layer that can be wrong quietly.

Everything downstream — cost, composition, leaderboards, unit economics — is arithmetic
over whatever these functions produce, so a field read from the wrong key does not fail,
it just reports a smaller number forever. That is what these cases guard.

Every event below is the real shape, captured from a live `claude -p --output-format
stream-json` run and from a salvaged session transcript on 2026-08-17. Do not tidy them
into something more convenient: their value is that the runtime actually emits them.

Run it directly, no framework needed:

    .venv/bin/python -m telemetry.normalize_test
"""
import sys

from telemetry.normalize import ClaudeCodeAdapter, LlmCall, ToolCall, elapsed_ms
from telemetry.store import canonical_model

# A real assistant event: usage on the message, a tool_use block, `parent_tool_use_id`
# on the envelope. Note `input_tokens: 2` against `cache_read_input_tokens: 16016` —
# the shape that makes cache reads the dominant cost and plain input a rounding error.
ASSISTANT = {
    "type": "assistant",
    "timestamp": "2026-08-17T10:00:00.000Z",
    "parent_tool_use_id": None,
    "message": {
        "model": "claude-opus-5",
        "content": [
            {"type": "tool_use", "id": "toolu_01", "name": "Bash",
             "input": {"command": "echo hi", "description": "say hi"}},
        ],
        "usage": {
            "input_tokens": 2,
            "output_tokens": 17,
            "cache_read_input_tokens": 16016,
            "cache_creation_input_tokens": 19177,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 0,
                "ephemeral_1h_input_tokens": 19177,
            },
        },
    },
}

TOOL_OK = {
    "type": "user",
    "timestamp": "2026-08-17T10:00:02.500Z",
    "message": {"content": [
        {"type": "tool_result", "tool_use_id": "toolu_01", "content": "hi"},
    ]},
}

TOOL_ERR = {
    "type": "user",
    "timestamp": "2026-08-17T10:00:02.500Z",
    "message": {"content": [
        {"type": "tool_result", "tool_use_id": "toolu_01", "is_error": True,
         "content": [{"type": "text", "text": "command not found"}]},
    ]},
}

failures: list[str] = []


def check(label: str, actual, expected) -> None:
    if actual != expected:
        failures.append(f"{label}: expected {expected!r}, got {actual!r}")


def one_call() -> LlmCall:
    """A run that made a single model call, with a Bash tool use in it."""
    adapter = ClaudeCodeAdapter("run-1")
    rows = adapter.feed(ASSISTANT)
    check("one llm_call per assistant event", len(rows), 1)
    return rows[0]


# --- the tokens ------------------------------------------------------------------
# Cache writes must land in the 1-hour bucket. The agent runtime asks for 1-hour
# caching, which is priced at 2x input against 1.25x for the 5-minute tier — collapsing
# the two understates the second-largest component of a run's cost by ~40%.
call = one_call()
check("input tokens", call.input_tokens, 2)
check("output tokens", call.output_tokens, 17)
check("cache reads captured", call.cache_read_tokens, 16016)
check("1h cache write bucketed", call.cache_write_1h_tokens, 19177)
check("5m bucket left empty", call.cache_write_5m_tokens, 0)
check("model recorded", call.model, "claude-opus-5")
check("turn starts at one", call.turn, 1)

# A payload with no `cache_creation` split falls back to the combined figure, and must
# go to the *cheaper* tier — an unknown split may never inflate the derived cost.
legacy = dict(ASSISTANT)
legacy["message"] = dict(ASSISTANT["message"])
legacy["message"]["usage"] = {
    "input_tokens": 5, "output_tokens": 6, "cache_creation_input_tokens": 100,
}
row = ClaudeCodeAdapter("run-legacy").feed(legacy)[0]
check("legacy write assumed 5m", (row.cache_write_5m_tokens, row.cache_write_1h_tokens),
      (100, 0))

# --- the split-message trap ------------------------------------------------------
# A session transcript writes one entry per content block and repeats the SAME `usage`
# object on each. Counting per entry rather than per message inflated real token totals
# by ~2x (observed up to 13 entries for one message across 37 production runs). Usage is
# counted once per `message.id`; tool_use blocks are still collected from every entry,
# because splitting them across entries is exactly how the transcript delivers them.
SPLIT_A = {
    "type": "assistant", "timestamp": "2026-08-17T10:00:00.000Z",
    "message": {"id": "msg_01", "model": "claude-sonnet-5",
                "content": [{"type": "thinking", "thinking": "…"}],
                "usage": {"input_tokens": 7, "output_tokens": 72,
                          "cache_read_input_tokens": 500}},
}
SPLIT_B = {
    "type": "assistant", "timestamp": "2026-08-17T10:00:00.500Z",
    "message": {"id": "msg_01", "model": "claude-sonnet-5",
                "content": [{"type": "tool_use", "id": "toolu_split", "name": "Read",
                             "input": {"file_path": "/a/b.py"}}],
                "usage": {"input_tokens": 7, "output_tokens": 72,
                          "cache_read_input_tokens": 500}},
}

adapter = ClaudeCodeAdapter("run-split")
first = adapter.feed(SPLIT_A)
second = adapter.feed(SPLIT_B)
check("first entry of a split message is billed", len(first), 1)
check("repeat of the same message id is not billed again",
      [r for r in second if isinstance(r, LlmCall)], [])
check("a split message counts as one turn", adapter.turn, 1)
# ...but its tool_use, which arrived on the *second* entry, must still be tracked.
resolved = adapter.feed({
    "type": "user", "timestamp": "2026-08-17T10:00:01.000Z",
    "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_split",
                             "content": "ok"}]},
})
check("tool_use from a repeated entry still becomes a row", len(resolved), 1)
check("and it is attributed to the right tool", resolved[0].tool, "Read")

# A distinct message id after a split is billed normally.
third = ClaudeCodeAdapter("run-split-2")
third.feed(SPLIT_A)
other = dict(SPLIT_A)
other["message"] = {**SPLIT_A["message"], "id": "msg_02"}
check("a different message id is billed", len(third.feed(other)), 1)
check("two messages, two turns", third.turn, 2)

# The live stream carries no `message.id` on some events; those cannot be deduplicated
# and must still be counted, or a real call would silently vanish from the ledger.
noid = ClaudeCodeAdapter("run-noid")
check("first usage with no id is counted", len(noid.feed(ASSISTANT)), 1)
check("second usage with no id is also counted", len(noid.feed(ASSISTANT)), 1)

# --- turns -----------------------------------------------------------------------
# Nothing in the stream carries a turn index; the adapter counts them. Without this,
# "the run hung at turn 41" is not a sentence the data can say.
adapter = ClaudeCodeAdapter("run-2")
adapter.feed(ASSISTANT)
adapter.feed(TOOL_OK)
second = adapter.feed(ASSISTANT)[0]
check("turn increments per model call", second.turn, 2)

# --- tool calls ------------------------------------------------------------------
adapter = ClaudeCodeAdapter("run-3")
adapter.feed(ASSISTANT)
tools = [r for r in adapter.feed(TOOL_OK) if isinstance(r, ToolCall)]
check("one tool_call per result", len(tools), 1)
tool = tools[0]
check("tool named", tool.tool, "Bash")
check("success recorded", tool.ok, True)
check("duration derived from the two timestamps", tool.duration_ms, 2500)
check("detail prefers the command", tool.detail, "echo hi")
check("tool keyed on the runtime's id", tool.id, "toolu_01")
check("attributed to the turn that requested it", tool.turn, 1)

adapter = ClaudeCodeAdapter("run-4")
adapter.feed(ASSISTANT)
failed = adapter.feed(TOOL_ERR)[0]
check("failure recorded", failed.ok, False)
check("error text kept", failed.error, "command not found")

# A tool result with no matching request (truncated transcript) yields nothing rather
# than a row with invented fields.
check("orphan result ignored", ClaudeCodeAdapter("run-5").feed(TOOL_OK), [])

# --- the run that died mid-tool --------------------------------------------------
# The diagnosis the old one-row-per-run ledger could not give. A tool that never
# returned is the signature of every timeout we have seen, so it must survive as a row.
adapter = ClaudeCodeAdapter("run-6")
adapter.feed(ASSISTANT)
stranded = adapter.flush()
check("unfinished tool emitted", len(stranded), 1)
check("unfinished tool marked failed", stranded[0].ok, False)
check("unfinished tool explains itself", stranded[0].error, "no result (run ended)")
check("unfinished tool has no duration", stranded[0].duration_ms, None)
check("flush is not repeatable", adapter.flush(), [])

# --- defensive reads -------------------------------------------------------------
# The event schema is not a contract. Anything unrecognised yields no rows; it never
# raises, because a parse error must not be able to end a run.
quiet = ClaudeCodeAdapter("run-7")
for junk in ({}, {"type": "result"}, {"type": "system", "subtype": "init"},
             {"type": "assistant"}, {"type": "user", "message": {"content": None}},
             {"type": "rate_limit_event"}, "not a dict"):
    check(f"no rows for {junk!r}", quiet.feed(junk), [])

# Timestamps go missing in salvaged transcripts; a duration we cannot compute is None,
# never zero — zero would silently read as an instant tool call in the leaderboard.
check("missing timestamp yields no duration", elapsed_ms(None, "2026-08-17T10:00:00Z"), None)
check("unparseable timestamp yields no duration", elapsed_ms("nonsense", "also"), None)

# --- model canonicalisation ------------------------------------------------------
# Deployment suffixes name a machine, not a price tier. If they are not stripped, every
# variant misses the price join and the run silently costs zero.
check("context suffix stripped", canonical_model("claude-opus-5[1m]"), "claude-opus-5")
check("date snapshot stripped", canonical_model("claude-haiku-4-5-20251001"),
      "claude-haiku-4-5")
check("plain id untouched", canonical_model("claude-opus-4-8"), "claude-opus-4-8")
check("no model is not a model", canonical_model(None), None)

if failures:
    print("\n".join(f"FAIL {f}" for f in failures))
    sys.exit(1)
print("normalize: all checks passed")
