"""The FACTORY_MEMORY priming receipt: what an agent reports it actually loaded.

`control.runner.parse_memory_receipt` is the pure counterpart to the prompt contract proved
in `control/prompt_profile_test.py`: it turns one machine-readable line back into
`{"indexed", "opened", "domains"}`, or into nothing at all when the line is missing,
malformed, or just ordinary agent chatter that happens to share no shape with a receipt.

The rest of this file proves the other half — that a valid receipt seen mid-stream turns
into telemetry rows (AC1), and that a telemetry layer which cannot write never takes the
run down with it (AC4).

Run it directly, no framework needed:

    .venv/bin/python -m control.memory_receipt_test
"""
import asyncio
import json
import sys

from telemetry.normalize import ClaudeCodeAdapter

from control import runner

fails: list[str] = []


def check(name, got, want):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}\n       got={got!r}")
    if not ok:
        fails.append(name)


MARKER = runner.MEMORY_RECEIPT_MARKER

# ---------- AC2: a valid receipt yields its indexed count, opened IDs, and domains

valid = f'{MARKER} {{"indexed": 9, "opened": ["mem_acac", "mem_5b24"], "domains": ["repository"]}}'
got = runner.parse_memory_receipt(valid)
check("a bare receipt parses", got is not None, True)
check("the indexed count comes through", got["indexed"], 9)
check("the opened ids come through, in order", got["opened"], ["mem_acac", "mem_5b24"])
check("the domains come through", got["domains"], ["repository"])

surrounded = (
    "some setup chatter\n"
    "$ .venv/bin/python -m control.memory_receipt_test\n"
    f"{valid}\n"
    "more chatter after it\n"
)
check("it is found among other run output", runner.parse_memory_receipt(surrounded), got)

leading_space = f"   {valid}   "
check("surrounding whitespace on the line is tolerated",
      runner.parse_memory_receipt(leading_space), got)

empty = f"{MARKER} {{\"indexed\": 0, \"opened\": [], \"domains\": []}}"
check("the explicit empty receipt parses to the same shape",
      runner.parse_memory_receipt(empty), runner.EMPTY_MEMORY_RECEIPT)
check("build_prompt's own empty-receipt text round-trips",
      runner.parse_memory_receipt(f"{MARKER} " + json.dumps(runner.EMPTY_MEMORY_RECEIPT)),
      runner.EMPTY_MEMORY_RECEIPT)

# ---------- AC3: malformed receipts and ordinary output produce no retrieval event, and
# none of them raise

no_marker = "I opened mem_acac and mem_5b24 from the repository domain."
check("prose that merely mentions ids is not a receipt", runner.parse_memory_receipt(no_marker), None)

bad_json = f"{MARKER} {{not json at all"
check("a marker with unparseable JSON after it yields nothing",
      runner.parse_memory_receipt(bad_json), None)

wrong_shape = f'{MARKER} {{"indexed": 9, "opened": "mem_acac", "domains": ["repository"]}}'
check("opened must be a list, not a bare string", runner.parse_memory_receipt(wrong_shape), None)

missing_field = f'{MARKER} {{"indexed": 9, "opened": ["mem_acac"]}}'
check("a receipt missing a required field is rejected", runner.parse_memory_receipt(missing_field), None)

wrong_types = f'{MARKER} {{"indexed": "nine", "opened": [], "domains": []}}'
check("indexed must be an int, not a string", runner.parse_memory_receipt(wrong_types), None)

bool_indexed = f'{MARKER} {{"indexed": true, "opened": [], "domains": []}}'
check("a bool is not an int here, even though Python thinks so",
      runner.parse_memory_receipt(bool_indexed), None)

negative = f'{MARKER} {{"indexed": -1, "opened": [], "domains": []}}'
check("a negative count is rejected", runner.parse_memory_receipt(negative), None)

non_string_items = f'{MARKER} {{"indexed": 1, "opened": [1, 2], "domains": []}}'
check("opened entries must be strings", runner.parse_memory_receipt(non_string_items), None)

lookalike_prefix = f"{MARKER}ISH not actually the marker {{\"indexed\": 1, \"opened\": [], \"domains\": []}}"
check("a line that only starts with the marker's letters is not the marker",
      runner.parse_memory_receipt(lookalike_prefix), None)

check("no receipt anywhere in ordinary output", runner.parse_memory_receipt("just a normal log\nnothing here"), None)
check("empty string does not raise", runner.parse_memory_receipt(""), None)
check("None-shaped input does not raise", runner.parse_memory_receipt(None), None)


# ---------- stream persists receipt / receipt persistence is best effort
#
# Nothing here talks to boxd or sqlite. `_stream` is driven the same way
# `control/manifest_test.py` drives it: over a canned stdout, with the recorder and the
# database stubbed, so what is under test is `_stream`'s own handling of the receipt line
# rather than a real agent or a real database.


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
        self.machines = self
        self._text = text

    def stream_exec(self, machine_id, command=None, env=None, close_stdin=True):
        return StubStream([Chunk(self._text)])


class StubRecorder:
    """Stands in for the telemetry recorder. `summary` is the real Claude adapter's, so
    the run's own usage is still checked here rather than stubbed into being right."""

    def __init__(self, run_id):
        self.dropped = 0
        self.adapter = ClaudeCodeAdapter(run_id)

    def use(self, events):
        return self.adapter.name

    def summary(self, event):
        return self.adapter.summary(event)

    async def feed(self, event):
        pass

    async def close(self):
        pass


RECEIPT = {"indexed": 3, "opened": ["mem_acac", "mem_5b24"], "domains": ["repository"]}
RECEIPT_LINE = f'{runner.MEMORY_RECEIPT_MARKER} {json.dumps(RECEIPT)}'

# The receipt printed as the agent's own assistant text, exactly how the prompt asks the
# agent to report it — mixed with ordinary turn text on either side, the way a real
# transcript would carry it.
ASSISTANT_EVENT = json.dumps({
    "type": "assistant",
    "message": {"content": [
        {"type": "text", "text": f"Primed from .mem/.\n{RECEIPT_LINE}\nStarting work now."},
    ]},
})
RESULT_EVENT = json.dumps({
    "type": "result", "subtype": "success", "num_turns": 2,
    "usage": {"input_tokens": 10, "output_tokens": 2},
})
STDOUT = f"{ASSISTANT_EVENT}\n{RESULT_EVENT}\n"


def run_stream(text, write_memory_reads, write_memory_receipt=None):
    """Drive `_stream` over one canned stdout, stubbing the recorder, the database, and the
    two telemetry writes the receipt would trigger."""
    log = Log()
    real_recorder = runner.Recorder
    real_update = runner.db.update_run
    real_write = runner.telemetry_store.write_memory_reads
    real_receipt = runner.telemetry_store.write_memory_receipt
    runner.Recorder = StubRecorder
    runner.db.update_run = lambda *a, **k: _noop()
    runner.telemetry_store.write_memory_reads = write_memory_reads
    runner.telemetry_store.write_memory_receipt = write_memory_receipt or _capture_nothing
    try:
        code, usage, manifest = asyncio.run(
            runner._stream(StubBoxd(text), "vm-1", {}, log, "run-1")
        )
    finally:
        runner.Recorder = real_recorder
        runner.db.update_run = real_update
        runner.telemetry_store.write_memory_reads = real_write
        runner.telemetry_store.write_memory_receipt = real_receipt
    return code, usage, log.lines


async def _capture_nothing(*args, **kwargs):
    return None


async def _noop():
    return None


# ---------- AC1: stream persists receipt

writes = []


async def capture(run_id, reads):
    writes.append((run_id, list(reads)))


code, usage, lines = run_stream(STDOUT, capture)

check("stream persists receipt: one write call for the run", len(writes), 1)
persisted_run_id, persisted_reads = writes[0]
check("stream persists receipt: written against the run that produced it",
      persisted_run_id, "run-1")
check("stream persists receipt: one row per opened memory record",
      [r[0] for r in persisted_reads], RECEIPT["opened"])
# The shape is the assertion. A read row is `(memory_id, ts)` and nothing else, so there is
# no per-record domain slot for the receipt's set of domains to be guessed into.
check("stream persists receipt: a read row is an id and a timestamp, nothing more",
      {len(r) for r in persisted_reads}, {2})
check("stream persists receipt: the run's result is unchanged",
      (code, usage.get("tokens_in"), usage.get("tokens_out")), (0, 10, 2))

writes.clear()
run_stream(f"{ASSISTANT_EVENT}\n{ASSISTANT_EVENT}\n{RESULT_EVENT}\n", capture)
check("stream persists receipt: a repeated receipt line in one run writes only once",
      len(writes), 1)


# ---------- AC2: the domains go to the run, whole

receipts = []


async def capture_receipt(run_id, indexed, domains, ts=None):
    receipts.append((run_id, indexed, list(domains)))


writes.clear()
run_stream(STDOUT, capture, capture_receipt)
check("stream persists receipt: exactly one receipt row for the run", len(receipts), 1)
check("stream persists receipt: it is the run that produced it", receipts[0][0], "run-1")
check("stream persists receipt: the indexed count is carried", receipts[0][1], RECEIPT["indexed"])
check("stream persists receipt: every domain the run drew from, not the first",
      receipts[0][2], RECEIPT["domains"])


# ---------- AC4: receipt persistence is best effort

async def explode(run_id, reads):
    raise RuntimeError("telemetry database is locked")


code, usage, lines = run_stream(STDOUT, explode)

check("best effort: a telemetry failure does not raise out of the stream", True, True)
check("best effort: the run still finishes with its usage",
      (code, usage.get("tokens_in"), usage.get("tokens_out")), (0, 10, 2))
check("best effort: the failure is noted in the run log",
      any("memory receipt not recorded" in ln for ln in lines), True)


# ---------- the empty receipt is a fact, not an absence
#
# `EMPTY_MEMORY_RECEIPT` exists so that "primed, opened nothing" and "never reached step 1"
# stop looking the same. Dropping its row because there were no records to attribute put them
# straight back to looking the same — and a run that indexed 40 records and opened none is the
# most interesting row in the table, because it is memory failing to earn its keep.

EMPTY_LINE = f'{runner.MEMORY_RECEIPT_MARKER} {json.dumps({"indexed": 12, "opened": [], "domains": []})}'
EMPTY_EVENT = json.dumps({
    "type": "assistant",
    "message": {"content": [{"type": "text", "text": f"Nothing here applies.\n{EMPTY_LINE}"}]},
})

writes.clear()
receipts.clear()
run_stream(f"{EMPTY_EVENT}\n{RESULT_EVENT}\n", capture, capture_receipt)
check("empty receipt: it still files a run-level row", len(receipts), 1)
check("empty receipt: the index size it saw is kept", receipts[0][1], 12)
check("empty receipt: no per-record rows, because there are no records", len(writes), 0)


# ---------- an empty receipt is provisional until something opens a record
#
# The build prompt spells the empty receipt out literally, as valid JSON, so an agent with no
# `.mem/` can copy it — which means anything echoing the prompt back through the stream (a
# `cat` of the prompt file, a subagent quoting its instructions) carries a parseable receipt.
# Stopping at the first match let that echo claim the run and discarded the real receipt
# printed seconds later.

writes.clear()
receipts.clear()
run_stream(f"{EMPTY_EVENT}\n{ASSISTANT_EVENT}\n{RESULT_EVENT}\n", capture, capture_receipt)
check("provisional: the real receipt still lands after an echoed empty one",
      [r[0] for r in writes[0][1]] if writes else None, RECEIPT["opened"])
check("provisional: the run-level row is corrected, not left at the echo",
      receipts[-1][1], RECEIPT["indexed"])
check("provisional: the echo and the correction, and nothing more", len(receipts), 2)

# The prompt's own empty-receipt text is the exact string at risk, so use it verbatim.
prompt_echo = json.dumps({
    "type": "user",
    "message": {"content": [{
        "type": "tool_result",
        "content": f"$ cat prompt.txt\n... print the explicit empty receipt instead of staying "
                   f"silent: {runner.MEMORY_RECEIPT_MARKER} "
                   f"{json.dumps(runner.EMPTY_MEMORY_RECEIPT)}\n",
    }]},
})
writes.clear()
receipts.clear()
run_stream(f"{prompt_echo}\n{ASSISTANT_EVENT}\n{RESULT_EVENT}\n", capture, capture_receipt)
check("provisional: a prompt echo does not stop the real receipt being recorded",
      [r[0] for r in writes[0][1]] if writes else None, RECEIPT["opened"])

# Once a run has said it opened something, it has answered. A later line — a retry's
# transcript, a subagent priming for itself — does not get to overwrite that.
writes.clear()
receipts.clear()
run_stream(f"{ASSISTANT_EVENT}\n{EMPTY_EVENT}\n{RESULT_EVENT}\n", capture, capture_receipt)
check("provisional: a later empty receipt does not overwrite a real one", len(receipts), 1)
check("provisional: and the real one is what was kept", receipts[0][1], RECEIPT["indexed"])


print()
print(f"{len(fails)} failed" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
