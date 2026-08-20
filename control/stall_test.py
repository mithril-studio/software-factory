"""Silence, and the three other things that look like it.

Two runs froze — one at 14 log lines for 63 minutes, one at 53 for 736 seconds — and the
factory learned nothing about either until the 90-minute `FACTORY_RUN_TIMEOUT` fired. Worse,
the watching around them turned a failed ssh into `0 lines / status=unknown` and read that as
a frozen log, so the first thing anyone "knew" was an artefact of the measurement.

`runner.bounded` is the honest version of that measurement, and this file is mostly about what
it must refuse to say. It watches the control plane's own stream — the same chunks the run log
is built from, so there is no second connection that can fail and be mistaken for silence —
and it distinguishes four endings that a cruder watchdog would collapse into one:

    a chunk arrives          -> not stalled, whatever it contains
    the stream ends          -> the run finished; nothing to report
    the stream raises        -> a crash. A dead stream is NOT a stalled one.
    nothing at all, for N    -> stalled, and only this one blames the golden

Only `runner._stream` is watched. `provision._stream` deliberately is not: an install is
legitimately silent for minutes and has no agent emitting events to be silent between.

No boxd, no database, no clock beyond a few hundredths of a second. Run it directly:

    .venv/bin/python -m control.stall_test
"""
import asyncio
import sys
import tempfile
from pathlib import Path

from control.runner import RunLog, Stalled, bounded, failure_reason

fails = []


def check(name, got, want=True):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}"
          + ("" if ok else f"\n       got={got!r} want={want!r}"))
    if not ok:
        fails.append(name)


tmp = tempfile.TemporaryDirectory()


def log_for(name):
    return RunLog(Path(tmp.name) / f"{name}.log")


# ---------- the stub streams
#
# Written as an explicit iterator rather than an async generator so a cancelled `__anext__`
# leaves nothing half-finalized behind it — the point here is the watchdog's behaviour, not
# the interpreter's tidying.

class Chunks:
    """Yields `items`, pausing `gap` seconds before each, then does `end`.

    `end` is where the interesting cases live: "stop" is a stream that finished, "hang" is one
    that went quiet without finishing, and "raise" is one that died.
    """

    def __init__(self, items, gap=0.0, end="stop"):
        self._items = list(items)
        self._gap = gap
        self._end = end

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._gap:
            await asyncio.sleep(self._gap)
        if self._items:
            return self._items.pop(0)
        if self._end == "hang":
            await asyncio.sleep(3600)
        if self._end == "raise":
            raise ConnectionResetError("the stream died")
        raise StopAsyncIteration


async def drain(chunks, idle, log):
    """Everything `bounded` yielded, or the exception it raised."""
    seen = []
    try:
        async for c in bounded(chunks, idle, log):
            seen.append(c)
            log.write(str(c))
    except Exception as exc:  # noqa: BLE001 - the exception is the finding
        return exc
    return seen


# ---------- a stream that goes quiet

stalled = asyncio.run(drain(Chunks(["a", "b"], end="hang"), 0.05, log_for("stalled")))
check("a stream that stops arriving is stalled", isinstance(stalled, Stalled), True)
check("and it says how long it waited", getattr(stalled, "idle", None), 0.05)
# The number a human recognises the failure by: the two real ones were "14 lines" and "53
# lines". Read off the run log itself, which is the only thing that knows.
check("and how far the run got before it went quiet", getattr(stalled, "lines", None), 2)
check("the message carries both, because a bare class name explains nothing",
      str(stalled), "no output for 0.05s after 2 lines")

# ---------- the three endings that are not a stall

ended = asyncio.run(drain(Chunks(["a", "b", "c"]), 0.05, log_for("ended")))
check("a stream that ends is a finished run, not a stalled one", ended, ["a", "b", "c"])

died = asyncio.run(drain(Chunks(["a"], end="raise"), 0.05, log_for("died")))
check("a stream that dies is a crash", isinstance(died, ConnectionResetError), True)
# The whole correction this file records: an unreachable thing must never be reported as a
# frozen one. They have different repairs, and only one of them points at the golden.
check("and emphatically not a stall", isinstance(died, Stalled), False)

# A gap under the threshold is a run doing something slow, which is most of what runs do —
# the longest legitimate silence is a single Bash call, bounded by FACTORY_BASH_MAX_TIMEOUT,
# which is why `Settings.idle_timeout` derives itself from that and never sits below it.
slow = asyncio.run(drain(Chunks(["a", "b"], gap=0.02), 0.2, log_for("slow")))
check("a slow stream is not a stalled one", slow, ["a", "b"])

off = asyncio.run(drain(Chunks(["a"], gap=0.1), 0, log_for("off")))
check("idle=0 switches the watchdog off entirely", off, ["a"])


# ---------- how the ending gets written down
#
# These strings are not cosmetic: they land in `runs.error`, which is what
# `db.snapshot_evidence` grades goldens from and what `agents.quarantined` then reads.

check("a stall is named as one", failure_reason(Stalled(2700, 14)),
      "stalled: no output for 2700s after 14 lines")
check("the run's own ceiling is a timeout, and says the number",
      failure_reason(asyncio.TimeoutError()).startswith("timed out after "), True)
check("everything else is a crash", failure_reason(ValueError("boom")), "crashed: boom")
# str() on some exceptions is empty, which used to record the least useful error possible.
check("including the silent ones, which fall back to the class",
      failure_reason(ValueError()), "crashed: ValueError")


print()
if fails:
    print(f"{len(fails)} failed: " + ", ".join(fails))
    sys.exit(1)
print("all passed")
