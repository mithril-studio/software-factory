"""The bridge from a live run to the tables.

One `Recorder` per run. The runner hands it every event it already parses for the log,
and it writes rows as the run happens rather than once at the end.

**Flushing per turn is the point of this file.** The old ledger wrote a run's usage in a
single UPDATE after the agent exited, so a timeout, a crash, or a dead VM lost the whole
record — which is why failed runs recorded `cost_usd = NULL` and roughly $13 of real
spend never appeared in our own numbers. Rows land as the turns complete; the most a
catastrophic failure can now cost is the turn in flight.

**Never raises.** Telemetry that can fail a run is worse than no telemetry, so every
write is guarded and a failure degrades to a missing row.
"""

from __future__ import annotations

from . import store
from .normalize import ClaudeCodeAdapter, LlmCall, ToolCall


class Recorder:
    def __init__(self, run_id: str) -> None:
        self.adapter = ClaudeCodeAdapter(run_id)
        self.run_id = run_id
        self._buffer: list[LlmCall | ToolCall] = []
        self.dropped = 0

    async def feed(self, event: dict) -> None:
        """Normalize one runtime event and buffer whatever it produced."""
        try:
            rows = self.adapter.feed(event)
        except Exception:  # noqa: BLE001 - a malformed event must not end a run
            self.dropped += 1
            return
        if not rows:
            return
        self._buffer.extend(rows)
        # An LlmCall marks the end of a turn: flush there, so the durable record never
        # trails the run by more than one turn.
        if any(isinstance(r, LlmCall) for r in rows):
            await self._flush()

    async def close(self) -> None:
        """Final flush, including tool calls the run died inside."""
        try:
            self._buffer.extend(self.adapter.flush())
        except Exception:  # noqa: BLE001
            self.dropped += 1
        await self._flush()

    async def totals(self) -> dict:
        """Token totals summed from the rows.

        The fallback when a run produces no `result` event — a timeout, a crash, a VM
        that vanished. Those runs used to record nothing at all; now their real spend is
        recoverable from what was written along the way.
        """
        try:
            usage = await store.usage_for_run(self.run_id)
        except Exception:  # noqa: BLE001
            return {}
        totals = usage.get("totals") or {}
        if not totals.get("calls"):
            return {}
        return {
            "tokens_in": totals.get("input_tokens"),
            "tokens_out": totals.get("output_tokens"),
            "cost_usd": round(totals.get("derived_cost_usd") or 0.0, 4) or None,
        }

    async def _flush(self) -> None:
        if not self._buffer:
            return
        batch, self._buffer = self._buffer, []
        try:
            await store.write(batch)
        except Exception:  # noqa: BLE001 - lose the batch, never the run
            self.dropped += len(batch)
