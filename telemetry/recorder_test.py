"""Which adapter a run records with, and when that can still be decided.

The recorder is where a manifest turns into a choice: a golden announces what its events
look like, and the run's rows are read by the adapter that matches — or by none, if this
layer has never heard of that agent. Two properties matter enough to pin down.

**The choice happens before the stream, or not at all.** An adapter is stateful over the
events it reads: turn counters, tool calls waiting for their results. Swapping one in
half-way through would hand a fresh adapter a stream whose beginning it never saw, and
the rows it then produced would be wrong in a way nothing downstream could detect. So a
switch after the first event is refused, loudly, and the adapter already reading keeps
reading.

**Nothing here can fail a run.** The manifest comes off a machine we did not build, so an
unknown agent, a missing string and a broken adapter all have to resolve to fewer rows
rather than to an exception.

No database: every event below produces no rows, so nothing is ever flushed. Run it
directly, no framework needed:

    .venv/bin/python -m telemetry.recorder_test
"""
import asyncio
import logging
import sys

from telemetry.normalize import ClaudeCodeAdapter, NullAdapter
from telemetry.recorder import Recorder

fails = []


def check(name, got, want=True):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n       got={got!r} want={want!r}"))
    if not ok:
        fails.append(name)


class Caught(logging.Handler):
    """Keeps whatever the recorder logged, so a refusal can be shown to be loud."""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record.getMessage())


caught = Caught()
logging.getLogger("factory.telemetry").addHandler(caught)

QUIET = {"type": "system", "subtype": "init"}   # a real event that produces no rows
RESULT = {"type": "result", "usage": {"input_tokens": 5, "output_tokens": 1},
          "total_cost_usd": 0.5}


# ---------- the default is what ran before manifests existed

check("the default adapter is claude", type(Recorder("run-1").adapter), ClaudeCodeAdapter)
check("an unknown agent records with the null adapter",
      type(Recorder("run-1", "pi").adapter), NullAdapter)


# ---------- the manifest may choose, up to the first event

recorder = Recorder("run-1")
check("the manifest switches the adapter", recorder.use("pi"), "null")
check("and the recorder is holding it", type(recorder.adapter), NullAdapter)
check("a manifest naming nothing falls back to the default", Recorder("run-1").use(None),
      "claude-code")
check("a manifest naming claude keeps claude", Recorder("run-1").use("claude-code"),
      "claude-code")

recorder = Recorder("run-1")
asyncio.run(recorder.feed(QUIET))
caught.records.clear()
check("a switch after the stream started is refused", recorder.use("pi"), "claude-code")
check("and the adapter reading the stream keeps reading", type(recorder.adapter),
      ClaudeCodeAdapter)
check("and the refusal is logged", [r for r in caught.records if "ignoring a switch" in r] != [])

# Re-stating the adapter already in use is not a switch, so it says nothing.
recorder = Recorder("run-1")
asyncio.run(recorder.feed(QUIET))
caught.records.clear()
check("restating the same adapter is silent", (recorder.use("claude-code"), caught.records),
      ("claude-code", []))


# ---------- the run's figures come from the adapter, and never raise

check("claude reports the run's figures", Recorder("run-1").summary(RESULT),
      {"tokens_in": 5, "tokens_out": 1, "cost_usd": 0.5})
check("claude reports nothing for an ordinary event", Recorder("run-1").summary(QUIET), {})
check("an agent with no adapter reports no figures rather than wrong ones",
      Recorder("run-1", "pi").summary(RESULT), {})


class Exploding:
    """An adapter written badly enough to raise. It may cost rows, never the run."""

    name = "exploding"

    def summary(self, event):
        raise RuntimeError("no")


recorder = Recorder("run-1")
recorder.adapter = Exploding()
check("an adapter that raises costs the figures and nothing else", recorder.summary(RESULT), {})

# Junk in the manifest's events field resolves to an adapter, never to an exception.
for events in (None, "", "   ", "CLAUDE-CODE", "claude code", "null"):
    check(f"events {events!r} still yields an adapter",
          bool(Recorder("run-1", events or "").adapter.name))

print()
print(f"{len(fails)} failed" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
