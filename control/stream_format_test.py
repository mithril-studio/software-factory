"""What the run log shows while an agent works — and what it must never swallow.

This log is the only window onto a run in flight. Everything else about a run arrives
after it is over, so a line that does not reach here is a thing nobody can watch, and the
worst version of that is silence that looks like calm: an agent whose events no formatter
recognises used to stream a *completely empty* log to the UI while working perfectly.
That is the failure this file exists to prevent, and it is the one you hit on the first
run of a new agent, which is precisely when you are watching.

So the rule the checks below pin down is: every JSON line the agent prints leaves a trace.
A recognised event leaves the line its formatter wrote; a recognised event with nothing
worth saying leaves none, because a run is half tool results nobody needs to read; and an
event from a vocabulary nobody has taught this log yet leaves a truncated raw line.

`_stream` is driven end to end against stubs, since a formatter that is right in isolation
and unreachable from the stream would pass a smaller test and lose the log anyway.

Run it directly, no framework needed:

    .venv/bin/python -m control.stream_format_test
"""
import asyncio
import json
import sys

from telemetry.normalize import ClaudeCodeAdapter

from control import runner
from control.runner import FORMATTERS, RAW_EVENT_MAX, format_event, stream_lines

fails = []


def check(name, got, want=True):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n       got={got!r} want={want!r}"))
    if not ok:
        fails.append(name)


# ---------- the stream harness

class Log:
    def __init__(self):
        self.lines = []

    def write(self, line):
        self.lines.append(line)


class Chunk:
    def __init__(self, text, is_stderr=False):
        self.data, self.is_stderr = text.encode(), is_stderr


class StubStream:
    def __init__(self, chunks):
        self._chunks, self.exit_code = chunks, 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def iter_chunks(self):
        for chunk in self._chunks:
            yield chunk


class StubBoxd:
    def __init__(self, text):
        self.machines, self._text = self, text

    def stream_exec(self, machine_id, command=None, env=None, close_stdin=True):
        return StubStream([Chunk(self._text)])


class StubRecorder:
    """The real Claude adapter behind a recorder that writes to no database."""

    def __init__(self, run_id, events="claude-code"):
        self.adapter = ClaudeCodeAdapter(run_id)
        self.dropped = 0

    def use(self, events):
        return self.adapter.name

    def summary(self, event):
        return self.adapter.summary(event)

    async def feed(self, event):
        pass

    async def close(self):
        pass


def log_of(*lines):
    """The run log `_stream` writes for a canned stdout."""
    log = Log()

    async def update_run(run_id, **fields):
        pass

    real_recorder, real_update = runner.Recorder, runner.db.update_run
    runner.Recorder, runner.db.update_run = StubRecorder, update_run
    try:
        asyncio.run(runner._stream(StubBoxd("".join(f"{ln}\n" for ln in lines)),
                                   "vm-1", {}, log, "run-1"))
    finally:
        runner.Recorder, runner.db.update_run = real_recorder, real_update
    return log.lines


# ---------- unrecognised event
# The shape a future agent arrives in: valid JSON, correlated, meaningful — and named
# something this log has never heard of.

UNKNOWN = {"type": "turn.completed", "turn": 4, "output": "wrote the failing test first"}
raw = json.dumps(UNKNOWN)

check("unrecognised event: produces exactly one line", len(stream_lines(UNKNOWN, raw)), 1)
check("unrecognised event: the line carries the event itself",
      "turn.completed" in stream_lines(UNKNOWN, raw)[0])
check("unrecognised event: reaches the run log through _stream", log_of(raw), stream_lines(UNKNOWN, raw))
check("unrecognised event: no formatter claims it", UNKNOWN["type"] in FORMATTERS, False)
check("unrecognised event: format_event alone says nothing about it", format_event(UNKNOWN), [])

# A run of a wholly unknown agent is watchable rather than blank — the whole point.
unknown_run = [json.dumps({"type": "turn.started", "turn": n}) for n in (1, 2, 3)]
check("unrecognised event: an entirely unknown stream still fills the log",
      len(log_of(*unknown_run)), 3)

# Truncated, because one chatty runtime must not bury a run in its own protocol.
long_event = {"type": "unknown.big", "blob": "x" * 5000}
line = stream_lines(long_event, json.dumps(long_event))[0]
check("unrecognised event: a huge one is truncated", len(line) <= RAW_EVENT_MAX + 20)
check("unrecognised event: truncation is visible", line.endswith("…"))


# ---------- recognised events keep their formatting, and their silences

SESSION = {"type": "system", "subtype": "init", "session_id": "s1"}
TOOL_USE = {"type": "assistant", "message": {"content": [
    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "npm test"}},
]}}
TOOL_OK = {"type": "user", "message": {"content": [
    {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
]}}
RESULT = {"type": "result", "subtype": "success", "num_turns": 2,
          "usage": {"input_tokens": 10, "output_tokens": 2}}

check("recognised: a session start is one line", log_of(json.dumps(SESSION)),
      ["[agent] session s1 started"])
check("recognised: a tool use keeps its hint", log_of(json.dumps(TOOL_USE)),
      ["[tool] Bash: npm test"])
check("recognised: a successful tool result stays silent", log_of(json.dumps(TOOL_OK)), [])
check("recognised: a quiet event is not logged raw",
      [ln for ln in log_of(json.dumps(TOOL_OK)) if "tool_result" in ln], [])
check("recognised: the final event still reports turns and tokens",
      log_of(json.dumps(RESULT)), ["[agent] finished: success | 2 turns | tokens in=10 out=2"])

# Not JSON at all: unchanged behaviour, the line goes through as the agent printed it.
check("recognised: a plain line is logged as it came", log_of("FACTORY: fetching origin"),
      ["FACTORY: fetching origin"])
# Broken JSON is also logged rather than dropped — it was already, and it stays that way.
check("recognised: a broken JSON line is still logged", log_of('{"type": "assist'),
      ['{"type": "assist'])

# Every formatter in the table is reachable and returns a list.
for kind, formatter in FORMATTERS.items():
    check(f"recognised: the {kind} formatter tolerates an empty event",
          isinstance(formatter({"type": kind}), list))

print()
print(f"{len(fails)} failed" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
