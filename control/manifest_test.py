"""The handshake between a golden and the control plane: one line of JSON on stdout.

The launch line used to be a constant in `runner`, which meant adding a second coding agent
meant editing the control plane. It now lives in the golden as `factory-agent`, and the
golden says what it is in `/etc/factory/agent.json` — so the control plane learns which
agent ran, and where that agent keeps its transcript, from the machine rather than from an
assumption compiled into it.

That makes the manifest a piece of input from outside, and every check below is the same
question from a different side: what happens when it is wrong? A golden with a hand-edited
manifest, a wrapper that printed the file with its newlines intact, an image that echoed the
prefix and nothing after it — none of those may end a run whose real work succeeded. The
manifest decides where to look for a transcript and who gets the credit; it decides nothing
about the code the agent wrote.

The other half is the stream. A manifest line is not an agent event, so it must be consumed
before the JSON branch: fed to the telemetry recorder it becomes a dropped event, and logged
raw it puts a line of machine handshake in the middle of a log a human reads.

Nothing here talks to boxd, sqlite or a real agent. Run it directly, no framework needed:

    .venv/bin/python -m control.manifest_test
"""
import asyncio
import json
import sys

from control import runner
from control.runner import (
    CLAUDE_TRANSCRIPT_GLOB,
    MANIFEST_PREFIX,
    REVIEW_SCRIPT,
    VM_SCRIPT,
    parse_manifest,
    transcript_glob,
)

fails = []


def check(name, got, want=True):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n       got={got!r} want={want!r}"))
    if not ok:
        fails.append(name)


MANIFEST = {
    "agent": "codex",
    "transcript": '"$HOME"/.codex/sessions/*.jsonl',
    "telemetry": "codex",
}
LINE = f"{MANIFEST_PREFIX} {json.dumps(MANIFEST)}"


# ---------- AC1: malformed
# Each of these is a golden somebody built wrong, and each must still complete its run.

check("malformed: a well-formed manifest parses", parse_manifest(LINE), MANIFEST)
check("malformed: the prefix with nothing after it", parse_manifest(MANIFEST_PREFIX), {})
check("malformed: the prefix with blank space after it", parse_manifest(f"{MANIFEST_PREFIX}   "), {})
check("malformed: json truncated mid-object",
      parse_manifest(f'{MANIFEST_PREFIX} {{"agent": "claude"'), {})
check("malformed: not json at all", parse_manifest(f"{MANIFEST_PREFIX} cat: no such file"), {})
# A wrapper that echoed the file without stripping newlines: the first line is all the
# stream parser ever sees, so the object never closes.
check("malformed: the file's newlines were not stripped", parse_manifest(f"{MANIFEST_PREFIX} {{"), {})
# Valid json, wrong shape. `.get` on a list raises, so this may not reach a caller.
check("malformed: json that is not an object", parse_manifest(f"{MANIFEST_PREFIX} [1, 2]"), {})
check("malformed: json that is a bare string", parse_manifest(f'{MANIFEST_PREFIX} "claude"'), {})
check("malformed: a line that is not a manifest at all", parse_manifest('{"type": "result"}'), {})
check("malformed: nothing at all", parse_manifest(""), {})
check("malformed: not even a string", parse_manifest(None), {})


# ---------- AC2: transcript
# The default is Claude Code's own path. It stays the default because the goldens that exist
# today announce nothing; it may not stay the assumption, because an agent that is not Claude
# Code has no ~/.claude to salvage from.

check("transcript: the manifest names one", transcript_glob(MANIFEST), MANIFEST["transcript"])
check("transcript: the manifest names none", transcript_glob({}), CLAUDE_TRANSCRIPT_GLOB)
check("transcript: there is no manifest", transcript_glob(None), CLAUDE_TRANSCRIPT_GLOB)
check("transcript: the manifest names an empty one",
      transcript_glob({"transcript": "  "}), CLAUDE_TRANSCRIPT_GLOB)
check("transcript: the manifest names something that is not a path",
      transcript_glob({"transcript": ["a", "b"]}), CLAUDE_TRANSCRIPT_GLOB)


class SalvageBoxd:
    """Enough boxd to see which command the salvage would have run in the VM."""

    def __init__(self):
        self.script = None
        self.machines = self

    async def exec(self, machine_id, script, timeout=None):
        self.script = script
        return type("Result", (), {"stdout": ""})()


def salvage_script(manifest):
    boxd = SalvageBoxd()
    asyncio.run(runner._salvage_transcript(boxd, "vm-1", "run-1", Log(), manifest))
    return boxd.script


# ---------- AC3: not an event
# The manifest line shares a stream with the agent's own stream-json events. It is neither
# one of them nor a stray line of agent output.

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
    """Stands in for the telemetry recorder, and keeps everything it was fed."""

    fed = []

    def __init__(self, run_id):
        StubRecorder.fed = []
        self.dropped = 0

    async def feed(self, event):
        StubRecorder.fed.append(event)

    async def close(self):
        pass


def run_stream(text):
    """Drive `_stream` over one canned stdout, with the recorder and the database stubbed."""
    writes = []

    async def update_run(run_id, **fields):
        writes.append(fields)

    log = Log()
    real_recorder, real_update = runner.Recorder, runner.db.update_run
    runner.Recorder, runner.db.update_run = StubRecorder, update_run
    try:
        code, usage, manifest = asyncio.run(
            runner._stream(StubBoxd(text), "vm-1", {}, log, "run-1")
        )
    finally:
        runner.Recorder, runner.db.update_run = real_recorder, real_update
    return code, usage, manifest, log.lines, writes, StubRecorder.fed


RESULT = json.dumps({"type": "result", "subtype": "success", "num_turns": 3,
                     "usage": {"input_tokens": 10, "output_tokens": 2}})
STDOUT = f'{LINE}\n{json.dumps({"type": "system", "subtype": "init", "session_id": "s1"})}\n{RESULT}\n'

code, usage, manifest, lines, writes, fed = run_stream(STDOUT)

check("not an event: the manifest never reaches the telemetry recorder",
      [e.get("type") for e in fed], ["system", "result"])
check("not an event: the manifest is not logged as a raw line",
      [ln for ln in lines if MANIFEST_PREFIX in ln], [])
check("not an event: the agent's own events still reach the recorder and the log",
      any("session s1 started" in ln for ln in lines))
check("not an event: the manifest is returned to the caller", manifest, MANIFEST)
check("not an event: the run still finishes with its usage",
      (code, usage.get("tokens_in"), usage.get("tokens_out")), (0, 10, 2))

recorded = {k: v for w in writes for k, v in w.items()}
check("not an event: the manifest is recorded against the run",
      json.loads(recorded.get("manifest", "null")), MANIFEST)
check("not an event: the agent that actually ran is written onto the run row",
      recorded.get("agent"), "codex")
check("not an event: the transcript is salvaged from where the manifest says",
      MANIFEST["transcript"] in salvage_script(manifest))

# A golden built before any of this: no manifest line, and everything still works.
code, usage, manifest, lines, writes, fed = run_stream(f"{RESULT}\n")
check("not an event: a golden that announces nothing yields no manifest", manifest, {})
check("not an event: a golden that announces nothing writes no agent",
      [k for w in writes for k in w if k in ("agent", "manifest")], [])
check("not an event: a golden that announces nothing falls back to the Claude transcript",
      CLAUDE_TRANSCRIPT_GLOB in salvage_script(manifest))

# A broken manifest is recorded as empty rather than dropping the run.
code, usage, manifest, lines, writes, fed = run_stream(f"{MANIFEST_PREFIX} not json\n{RESULT}\n")
check("not an event: a broken manifest still lets the run finish", (code, manifest), (0, {}))
check("not an event: a broken manifest leaves the requested agent alone",
      [k for w in writes for k in w if k == "agent"], [])
check("not an event: a broken manifest says so in the log",
      any("manifest unreadable" in ln for ln in lines))


# ---------- AC4: both scripts launch through the wrapper

for name, script in (("build", VM_SCRIPT), ("review", REVIEW_SCRIPT)):
    check(f"{name}: hands off to the wrapper as its last act",
          script.rstrip().endswith("exec factory-agent"))
    check(f"{name}: announces the manifest immediately before handing off",
          script.rstrip().splitlines()[-2].startswith(f'echo "{MANIFEST_PREFIX} '))
    check(f"{name}: strips the manifest's newlines, so it stays one line",
          "tr -d '\\n' < /etc/factory/agent.json" in script)
    check(f"{name}: keeps one pre-wrapper fallback", script.count("command -v factory-agent"), 1)
    check(f"{name}: the fallback exits with the agent's own status", "; exit $?; }" in script)
    # The prompt is multi-kilobyte and contains arbitrary quoting, so nothing may re-quote
    # it through another shell — an executable on PATH is what makes that impossible.
    check(f"{name}: the manifest is never parsed or re-quoted in shell",
          [bad for bad in ("eval", "jq ", "sh -c") if bad in script], [])

print()
print(f"{len(fails)} failed" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
