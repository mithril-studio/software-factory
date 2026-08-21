"""Does the memory validator actually catch a bad `.mem/`, and leave a good one alone?

`control/memory.py` is the one thing standing between a malformed or unscoped record and a
future agent trusting it as fact. Each check below builds a throwaway `.mem/` on disk —
sometimes deliberately broken one way — and asserts on the exact finding it produces, so a
future edit that silently stops catching a failure class shows up here rather than in a run
that quietly absorbed bad memory.

Nothing here needs credentials, a VM or a database. Run it directly, no framework needed:

    .venv/bin/python -m control.memory_test
"""
import json
import sys
import tempfile
from pathlib import Path

from control.memory import validate

fails = []


def check(name, got, want=True):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n       got={got!r} want={want!r}"))
    if not ok:
        fails.append(name)


def any_message_contains(findings, needle):
    return any(needle in str(f) for f in findings)


def record(**overrides):
    base = {
        "id": "mem_0001",
        "domain": "widgets",
        "type": "convention",
        "title": "Widgets are always blue",
        "body": "Every widget in this repo renders blue by convention.",
        "resolution": None,
        "evidence": {
            "files": ["widget.py"],
            "dirs": [],
            "branch": "main",
            "issues": [],
            "run": None,
        },
        "provenance": {
            "author": "agent:claude-code",
            "backend": "boxd",
            "created_at": "2026-08-19T00:00:00Z",
        },
        "status": "active",
        "supersedes": None,
        "confidence": "high",
        "hits": 0,
    }
    base.update(overrides)
    return base


def index_line(rec):
    return {
        "id": rec["id"],
        "domain": rec["domain"],
        "type": rec["type"],
        "title": rec["title"],
        "files": rec["evidence"]["files"] + rec["evidence"]["dirs"],
    }


def build_repo(tmp, *, index_lines=None, domain_records=None, archive_records=None, touch_files=()):
    """Write a `.mem/` (plus any files evidence paths need to resolve) under `tmp`."""
    repo = Path(tmp)
    mem = repo / ".mem"
    (mem / "domains").mkdir(parents=True)
    (mem / "archive").mkdir(parents=True)
    for f in touch_files:
        path = repo / f
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")

    if index_lines is not None:
        (mem / "index.jsonl").write_text(
            "\n".join(json.dumps(line) if not isinstance(line, str) else line for line in index_lines) + "\n"
        )

    domain_records = domain_records or {}
    for domain, lines in domain_records.items():
        (mem / "domains" / f"{domain}.jsonl").write_text(
            "\n".join(json.dumps(line) if not isinstance(line, str) else line for line in lines) + "\n"
        )

    archive_records = archive_records or {}
    for domain, lines in archive_records.items():
        (mem / "archive" / f"{domain}.jsonl").write_text(
            "\n".join(json.dumps(line) if not isinstance(line, str) else line for line in lines) + "\n"
        )
    return repo


# ---------- valid store

with tempfile.TemporaryDirectory() as tmp:
    rec = record()
    repo = build_repo(
        tmp,
        index_lines=[index_line(rec)],
        domain_records={"widgets": [rec]},
        touch_files=["widget.py"],
    )
    findings = validate(str(repo))
    check("valid store: a well-formed indexed store has no findings", findings, [])

# A retired id, correctly moved to archive/ and superseded by a live record, still validates.
with tempfile.TemporaryDirectory() as tmp:
    old = record(id="mem_0001", status="superseded")
    new = record(id="mem_0002", supersedes="mem_0001")
    repo = build_repo(
        tmp,
        index_lines=[index_line(new)],
        domain_records={"widgets": [new]},
        archive_records={"widgets": [old]},
        touch_files=["widget.py"],
    )
    findings = validate(str(repo))
    check("valid store: a correctly-archived supersession has no findings", findings, [])

# No .mem/ at all is nothing to validate, not a failure.
with tempfile.TemporaryDirectory() as tmp:
    findings = validate(tmp)
    check("valid store: no .mem/ directory produces no findings", findings, [])


# ---------- invalid stores

# Malformed JSON in a domain file names the file and line.
with tempfile.TemporaryDirectory() as tmp:
    rec = record()
    mem = Path(tmp) / ".mem"
    (mem / "domains").mkdir(parents=True)
    (mem / "index.jsonl").write_text(json.dumps(index_line(rec)) + "\n")
    (mem / "domains" / "widgets.jsonl").write_text("{not valid json\n")
    findings = validate(tmp)
    check("invalid stores: malformed JSON is reported", len(findings) > 0, True)
    check("invalid stores: malformed JSON names the offending file",
          any_message_contains(findings, "domains/widgets.jsonl"), True)

# Duplicate ID across two records in the same domain file.
with tempfile.TemporaryDirectory() as tmp:
    a = record(id="mem_dupe")
    b = record(id="mem_dupe", title="A different title, same id")
    repo = build_repo(
        tmp,
        index_lines=[index_line(a)],
        domain_records={"widgets": [a, b]},
        touch_files=["widget.py"],
    )
    findings = validate(str(repo))
    check("invalid stores: a duplicate id is reported", any_message_contains(findings, "duplicate id 'mem_dupe'"), True)

# Index entry with no matching record ("missing index entries" — a record the index claims
# exists but that was never written to a domain file).
with tempfile.TemporaryDirectory() as tmp:
    rec = record()
    ghost = index_line(record(id="mem_ghost"))
    repo = build_repo(
        tmp,
        index_lines=[index_line(rec), ghost],
        domain_records={"widgets": [rec]},
        touch_files=["widget.py"],
    )
    findings = validate(str(repo))
    check("invalid stores: an index entry with no matching record is reported",
          any_message_contains(findings, "mem_ghost' has no matching active record"), True)

# The reverse: an active record with no index entry at all.
with tempfile.TemporaryDirectory() as tmp:
    rec = record()
    repo = build_repo(
        tmp,
        index_lines=[],
        domain_records={"widgets": [rec]},
        touch_files=["widget.py"],
    )
    findings = validate(str(repo))
    check("invalid stores: an active record missing from the index is reported",
          any_message_contains(findings, "mem_0001' is active but has no entry in"), True)

# Mismatched domain: the record's own `domain` field disagrees with the file it lives in.
with tempfile.TemporaryDirectory() as tmp:
    rec = record(domain="gadgets")
    repo = build_repo(
        tmp,
        index_lines=[index_line(rec)],
        domain_records={"widgets": [rec]},
        touch_files=["widget.py"],
    )
    findings = validate(str(repo))
    check("invalid stores: a record whose domain disagrees with its file is reported",
          any_message_contains(findings, "declares domain 'gadgets'"), True)

# supersedes pointing at an id that does not exist anywhere in the store.
with tempfile.TemporaryDirectory() as tmp:
    rec = record(supersedes="mem_nowhere")
    repo = build_repo(
        tmp,
        index_lines=[index_line(rec)],
        domain_records={"widgets": [rec]},
        touch_files=["widget.py"],
    )
    findings = validate(str(repo))
    check("invalid stores: supersedes referencing an unknown id is reported",
          any_message_contains(findings, "mem_nowhere', which does not exist"), True)

# A retired record left behind in domains/ instead of moved to archive/.
with tempfile.TemporaryDirectory() as tmp:
    rec = record(status="superseded")
    repo = build_repo(
        tmp,
        index_lines=[],
        domain_records={"widgets": [rec]},
        touch_files=["widget.py"],
    )
    findings = validate(str(repo))
    check("invalid stores: a superseded record still checked into domains/ is reported",
          any_message_contains(findings, "checked into domains/, not archive/"), True)

# Missing required fields on a record.
with tempfile.TemporaryDirectory() as tmp:
    rec = record()
    del rec["confidence"]
    repo = build_repo(
        tmp,
        index_lines=[index_line(rec)],
        domain_records={"widgets": [rec]},
        touch_files=["widget.py"],
    )
    findings = validate(str(repo))
    check("invalid stores: a record missing a required field is reported",
          any_message_contains(findings, "missing field(s): confidence"), True)

# An oversized index line.
with tempfile.TemporaryDirectory() as tmp:
    rec = record(title="x" * 400)
    repo = build_repo(
        tmp,
        index_lines=[index_line(rec)],
        domain_records={"widgets": [rec]},
        touch_files=["widget.py"],
    )
    findings = validate(str(repo))
    check("invalid stores: an oversized index line is reported",
          any_message_contains(findings, "byte budget"), True)


# ---------- missing evidence

# AC3: an active, code-local record with no evidence paths at all.
with tempfile.TemporaryDirectory() as tmp:
    rec = record(evidence={"files": [], "dirs": [], "branch": "main", "issues": [], "run": None})
    repo = build_repo(
        tmp,
        index_lines=[index_line(rec)],
        domain_records={"widgets": [rec]},
    )
    findings = validate(str(repo))
    check("missing evidence: an active record with no evidence paths at all is reported",
          any_message_contains(findings, "names no evidence path"), True)

# An active, code-local record whose evidence paths don't resolve to anything on disk.
with tempfile.TemporaryDirectory() as tmp:
    rec = record(evidence={
        "files": ["nowhere/nothing.py"], "dirs": [], "branch": "main", "issues": [], "run": None,
    })
    repo = build_repo(
        tmp,
        index_lines=[index_line(rec)],
        domain_records={"widgets": [rec]},
    )
    findings = validate(str(repo))
    check("missing evidence: evidence paths that don't resolve on disk are reported",
          any_message_contains(findings, "do not resolve to anything"), True)

# A `_universal` record needs no evidence path at all — it has no code anchor by design.
with tempfile.TemporaryDirectory() as tmp:
    rec = record(
        domain="_universal",
        evidence={"files": [], "dirs": [], "branch": "main", "issues": [], "run": None},
    )
    repo = build_repo(
        tmp,
        index_lines=[index_line(rec)],
        domain_records={"_universal": [rec]},
    )
    findings = validate(str(repo))
    check("missing evidence: a _universal record with no evidence path is not reported", findings, [])


# ---------- the index's `files`, which is the field priming actually selects on
#
# §3.2 defines an index line's `files` as `evidence.files` plus `evidence.dirs`, merged.
# `domain`, `type` and `title` are copied verbatim and drift there is visible to anyone
# reading both lines; `files` is derived, so it silently goes stale when a record's evidence
# grows. §4 step 3 matches a working set against the index and nothing else, so a path present
# in the record but missing from its index line means the record is simply never retrieved for
# that path — a store that validates clean and quietly under-serves. This repo's own `.mem/`
# had five such lines.

with tempfile.TemporaryDirectory() as tmp:
    rec = record(evidence={
        "files": ["widget.py", "widget_test.py"], "dirs": [], "branch": "main",
        "issues": [], "run": None,
    })
    stale = index_line(rec)
    stale["files"] = ["widget.py"]  # the line as it was before the record gained a second file
    repo = build_repo(
        tmp,
        index_lines=[stale],
        domain_records={"widgets": [rec]},
        touch_files=["widget.py", "widget_test.py"],
    )
    findings = validate(str(repo))
    check("index files: a path the record names but the index omits is reported",
          any_message_contains(findings, "omits evidence path(s)") and
          any_message_contains(findings, "widget_test.py"), True)

with tempfile.TemporaryDirectory() as tmp:
    rec = record()
    invented = index_line(rec)
    invented["files"] = rec["evidence"]["files"] + ["not/in/the/record.py"]
    repo = build_repo(
        tmp,
        index_lines=[invented],
        domain_records={"widgets": [rec]},
        touch_files=["widget.py"],
    )
    findings = validate(str(repo))
    check("index files: a path the index invents is reported",
          any_message_contains(findings, "not/in/the/record.py"), True)

# A record's `dirs` belong in the index line too — they are half of what §3.2 merges, and the
# broadest half: a `dirs` entry left out takes every file under it out of retrieval range.
with tempfile.TemporaryDirectory() as tmp:
    rec = record(evidence={
        "files": ["widget.py"], "dirs": ["widgets"], "branch": "main", "issues": [], "run": None,
    })
    without_dirs = index_line(rec)
    without_dirs["files"] = ["widget.py"]
    repo = build_repo(
        tmp,
        index_lines=[without_dirs],
        domain_records={"widgets": [rec]},
        touch_files=["widget.py", "widgets/a.py"],
    )
    findings = validate(str(repo))
    check("index files: evidence.dirs missing from the index line is reported",
          any_message_contains(findings, "'widgets'"), True)

# Order is not part of the contract; the same set written differently is the same line.
with tempfile.TemporaryDirectory() as tmp:
    rec = record(evidence={
        "files": ["widget.py"], "dirs": ["widgets"], "branch": "main", "issues": [], "run": None,
    })
    reordered = index_line(rec)
    reordered["files"] = ["widgets", "widget.py"]
    repo = build_repo(
        tmp,
        index_lines=[reordered],
        domain_records={"widgets": [rec]},
        touch_files=["widget.py", "widgets/a.py"],
    )
    check("index files: the same paths in a different order are not a finding",
          validate(str(repo)), [])


# ---------- one index line per record (§3.2)
#
# Two lines for one id is not untidy, it is ambiguous: whichever the reader reaches first
# decides what it believes about the record, and nothing reconciles the other.

with tempfile.TemporaryDirectory() as tmp:
    rec = record()
    second = index_line(rec)
    second["title"] = "Widgets are always green"
    repo = build_repo(
        tmp,
        index_lines=[index_line(rec), second],
        domain_records={"widgets": [rec]},
        touch_files=["widget.py"],
    )
    findings = validate(str(repo))
    check("duplicate index entry: a second line for one id is reported",
          any_message_contains(findings, "appears twice"), True)


print()
if fails:
    print(f"{len(fails)} failed: " + ", ".join(fails))
    sys.exit(1)
print("all passed")
