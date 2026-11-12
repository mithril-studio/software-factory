"""A deterministic validator for `.mem/`, the repository memory store.

The memory skill (`memory-skills/memory/SKILL.md` in mithril-studio/agent-skills, installed
into every golden) describes a schema by convention: one
short line per record in `.mem/index.jsonl`, the full record in `.mem/domains/<domain>.jsonl`,
and retired records moved wholesale into `.mem/archive/<domain>.jsonl`. Nothing enforces that
shape except discipline, and a store an agent trusts without checking is a store that can feed
a malformed, orphaned, or unscoped record into a future run's context as if it were fact.

This module is that check. It is pure: it reads `.mem/`, reports what is wrong, and changes
nothing. It has no model in it and makes no judgment calls about which memories are worth
keeping — that is the skill's job, not this one's.

Run it directly, no framework needed:

    python -m control.memory validate [repo-path]

Exit code is 0 with no findings, 1 with at least one.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

INDEX_FILE = "index.jsonl"
DOMAINS_DIR = "domains"
ARCHIVE_DIR = "archive"
UNIVERSAL_DOMAIN = "_universal"

REQUIRED_RECORD_FIELDS = (
    "id", "domain", "type", "title", "body", "resolution",
    "evidence", "provenance", "status", "supersedes", "confidence", "hits",
)
REQUIRED_INDEX_FIELDS = ("id", "domain", "type", "title", "files")
REQUIRED_EVIDENCE_FIELDS = ("files", "dirs", "branch", "issues", "run")
REQUIRED_PROVENANCE_FIELDS = ("author", "backend", "created_at")

VALID_TYPES = {"convention", "failure", "pattern", "decision", "reference"}
VALID_CONFIDENCE = {"high", "medium", "low"}
ACTIVE_STATUS = "active"
RETIRED_STATUS = {"superseded", "deprecated"}
VALID_STATUS = {ACTIVE_STATUS} | RETIRED_STATUS

# `.mem/index.jsonl` §3.2: "Keep the line under ~350 bytes."
MAX_INDEX_LINE_BYTES = 350


@dataclass
class Finding:
    """One thing wrong with the store, anchored to where it was found."""

    path: str
    message: str
    line: int | None = None

    def __str__(self) -> str:
        loc = f"{self.path}:{self.line}" if self.line is not None else self.path
        return f"{loc}: {self.message}"


@dataclass
class _Record:
    id: str
    domain_file: str  # domain the *file* claims, from its own filename
    obj: dict
    path: str
    line: int
    retired: bool  # found under archive/, expected to carry a retired status


def _relpath(repo: Path, path: Path) -> str:
    try:
        return str(path.relative_to(repo))
    except ValueError:
        return str(path)


def _read_jsonl(path: Path, repo: Path,
                findings: list[Finding]) -> list[tuple[int, str, dict]]:
    """Parse a JSONL file, reporting one finding per unparseable line.

    Each entry carries the raw line alongside the parsed object, because the index's
    byte-budget check (§3.2) is about the bytes on disk, not about what they decode to.
    Handing it back here is what keeps the caller from re-reading the whole file once per
    line to find them again.
    """
    rel = _relpath(repo, path)
    entries = []
    for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            findings.append(Finding(rel, f"invalid JSON ({exc.msg})", lineno))
            continue
        if not isinstance(obj, dict):
            findings.append(Finding(rel, "line is valid JSON but not an object", lineno))
            continue
        entries.append((lineno, raw, obj))
    return entries


def _check_index_entry(rel: str, lineno: int, raw: str, obj: dict, findings: list[Finding]) -> None:
    missing = [f for f in REQUIRED_INDEX_FIELDS if f not in obj]
    if missing:
        findings.append(Finding(rel, f"index entry missing field(s): {', '.join(missing)}", lineno))
        return
    if not isinstance(obj.get("files"), list):
        findings.append(Finding(rel, "index entry 'files' must be a list", lineno))
    if obj.get("type") not in VALID_TYPES:
        findings.append(Finding(rel, f"index entry has unknown type {obj.get('type')!r}", lineno))
    size = len(raw.encode("utf-8"))
    if size > MAX_INDEX_LINE_BYTES:
        findings.append(Finding(
            rel, f"index line for {obj.get('id', '?')!r} is {size} bytes, over the "
                 f"{MAX_INDEX_LINE_BYTES}-byte budget", lineno,
        ))


def _check_record_shape(rel: str, lineno: int, obj: dict, domain_file: str,
                         findings: list[Finding]) -> None:
    missing = [f for f in REQUIRED_RECORD_FIELDS if f not in obj]
    if missing:
        findings.append(Finding(
            rel, f"record {obj.get('id', '?')!r} missing field(s): {', '.join(missing)}", lineno,
        ))
        return

    rid = obj["id"]
    if obj.get("domain") != domain_file:
        findings.append(Finding(
            rel, f"record {rid!r} declares domain {obj.get('domain')!r}, "
                 f"which disagrees with its file ({domain_file!r})", lineno,
        ))
    if obj.get("type") not in VALID_TYPES:
        findings.append(Finding(rel, f"record {rid!r} has unknown type {obj.get('type')!r}", lineno))
    if obj.get("status") not in VALID_STATUS:
        findings.append(Finding(rel, f"record {rid!r} has unknown status {obj.get('status')!r}", lineno))
    if obj.get("confidence") not in VALID_CONFIDENCE:
        findings.append(Finding(
            rel, f"record {rid!r} has unknown confidence {obj.get('confidence')!r}", lineno,
        ))
    if obj.get("type") == "failure" and not obj.get("resolution"):
        findings.append(Finding(rel, f"record {rid!r} is type 'failure' but has no resolution", lineno))

    evidence = obj.get("evidence")
    if not isinstance(evidence, dict):
        findings.append(Finding(rel, f"record {rid!r} has no evidence object", lineno))
    else:
        missing_ev = [f for f in REQUIRED_EVIDENCE_FIELDS if f not in evidence]
        if missing_ev:
            findings.append(Finding(
                rel, f"record {rid!r} evidence missing field(s): {', '.join(missing_ev)}", lineno,
            ))
        for key in ("files", "dirs"):
            if key in evidence and not isinstance(evidence[key], list):
                findings.append(Finding(rel, f"record {rid!r} evidence.{key} must be a list", lineno))

    provenance = obj.get("provenance")
    if not isinstance(provenance, dict):
        findings.append(Finding(rel, f"record {rid!r} has no provenance object", lineno))
    else:
        missing_prov = [f for f in REQUIRED_PROVENANCE_FIELDS if f not in provenance]
        if missing_prov:
            findings.append(Finding(
                rel, f"record {rid!r} provenance missing field(s): {', '.join(missing_prov)}", lineno,
            ))


def _evidence_paths(obj: dict) -> list[str]:
    evidence = obj.get("evidence")
    if not isinstance(evidence, dict):
        return []
    paths = []
    for key in ("files", "dirs"):
        value = evidence.get(key)
        if isinstance(value, list):
            paths.extend(p for p in value if isinstance(p, str))
    return paths


def _path_is_usable(repo: Path, pattern: str) -> bool:
    """A literal path that exists, or a glob that matches something, under `repo`."""
    if (repo / pattern).exists():
        return True
    if any(ch in pattern for ch in "*?["):
        return any(repo.glob(pattern))
    return False


def _check_evidence(rel: str, lineno: int, obj: dict, repo: Path, findings: list[Finding]) -> None:
    """AC3: an active, non-universal record must name at least one path that resolves."""
    if obj.get("status") != ACTIVE_STATUS:
        return
    if obj.get("domain") == UNIVERSAL_DOMAIN:
        return
    paths = _evidence_paths(obj)
    if not paths:
        findings.append(Finding(
            rel, f"record {obj.get('id', '?')!r} is active and code-local but names no "
                 f"evidence path", lineno,
        ))
        return
    if not any(_path_is_usable(repo, p) for p in paths):
        findings.append(Finding(
            rel, f"record {obj.get('id', '?')!r} evidence paths do not resolve to anything "
                 f"in the repo: {paths}", lineno,
        ))


def _collect_records(mem: Path, repo: Path, subdir: str, retired: bool,
                      findings: list[Finding]) -> list[_Record]:
    directory = mem / subdir
    if not directory.is_dir():
        return []
    out = []
    for path in sorted(directory.glob("*.jsonl")):
        rel = _relpath(repo, path)
        domain_file = path.stem
        for lineno, _raw, obj in _read_jsonl(path, repo, findings):
            _check_record_shape(rel, lineno, obj, domain_file, findings)
            if not retired:
                _check_evidence(rel, lineno, obj, repo, findings)
                if "status" in obj and obj["status"] != ACTIVE_STATUS:
                    findings.append(Finding(
                        rel, f"record {obj.get('id', '?')!r} has status "
                             f"{obj['status']!r} but is checked into domains/, not archive/",
                        lineno,
                    ))
            elif "status" in obj and obj["status"] == ACTIVE_STATUS:
                findings.append(Finding(
                    rel, f"record {obj.get('id', '?')!r} is archived but still marked active",
                    lineno,
                ))
            if "id" in obj:
                out.append(_Record(obj["id"], domain_file, obj, rel, lineno, retired))
    return out


def _check_unique_ids(records: list[_Record], findings: list[Finding]) -> None:
    seen: dict[str, _Record] = {}
    for rec in records:
        prior = seen.get(rec.id)
        if prior is None:
            seen[rec.id] = rec
            continue
        if prior.retired == rec.retired:
            findings.append(Finding(
                rec.path, f"duplicate id {rec.id!r}, first seen at {prior.path}:{prior.line}",
                rec.line,
            ))
        else:
            findings.append(Finding(
                rec.path, f"id {rec.id!r} is both live and archived — {'archived' if prior.retired else 'live'} "
                          f"at {prior.path}:{prior.line}, {'archived' if rec.retired else 'live'} here; a "
                          f"retired record moves to archive/, it is not copied there", rec.line,
            ))


def _index_files(obj: dict) -> list[str]:
    """§3.2: an index line's `files` is `evidence.files` plus `evidence.dirs`, merged.

    Order is not part of the contract, so this returns the list as written and the caller
    compares it as a set — but nothing may be dropped, because this list is the only thing
    priming (§4 step 3) matches a working set against.
    """
    return _evidence_paths(obj)


def _check_index_consistency(index_entries: list[tuple[int, dict]], live: list[_Record],
                              index_rel: str, findings: list[Finding]) -> None:
    live_by_id = {r.id: r for r in live if isinstance(r.obj, dict) and r.obj.get("id") == r.id}
    indexed_ids: dict[str, int] = {}
    for lineno, obj in index_entries:
        rid = obj.get("id")
        if rid is None:
            continue
        # Exactly one line per active record (§3.2). Two lines for one id is not merely
        # untidy: whichever the reader hits first decides what it believes about a record,
        # and the other is a second answer nothing reconciles.
        if rid in indexed_ids:
            findings.append(Finding(
                index_rel, f"index entry {rid!r} appears twice, first at line "
                           f"{indexed_ids[rid]}; a record gets exactly one index line", lineno,
            ))
            continue
        indexed_ids[rid] = lineno
        record = live_by_id.get(rid)
        if record is None:
            findings.append(Finding(
                index_rel, f"index entry {rid!r} has no matching active record in domains/", lineno,
            ))
            continue
        for field in ("domain", "type", "title"):
            if obj.get(field) != record.obj.get(field):
                findings.append(Finding(
                    index_rel, f"index entry {rid!r} {field}={obj.get(field)!r} disagrees with "
                               f"record {field}={record.obj.get(field)!r} ({record.path}:{record.line})",
                    lineno,
                ))
        # The one field where drift is silent and expensive. `domain`, `type` and `title` are
        # copied verbatim and a mismatch is visible to anyone reading both lines; `files` is
        # derived, so it goes stale the moment a record's evidence grows and the index line
        # does not. Priming selects on the index alone, so a path that is in the record but
        # not the index means the record is simply never retrieved for that path — the store
        # looks healthy and quietly under-serves. Checked here so it cannot happen twice.
        entry_files = obj.get("files")
        if isinstance(entry_files, list):
            want = _index_files(record.obj)
            have = [f for f in entry_files if isinstance(f, str)]
            dropped = sorted(set(want) - set(have))
            invented = sorted(set(have) - set(want))
            if dropped:
                findings.append(Finding(
                    index_rel, f"index entry {rid!r} omits evidence path(s) the record names: "
                               f"{dropped} — priming matches on this list, so the record is "
                               f"invisible to work touching them", lineno,
                ))
            if invented:
                findings.append(Finding(
                    index_rel, f"index entry {rid!r} names path(s) the record's evidence does "
                               f"not: {invented}", lineno,
                ))

    for rec in live:
        if rec.obj.get("status") == ACTIVE_STATUS and rec.id not in indexed_ids:
            findings.append(Finding(
                rec.path, f"record {rec.id!r} is active but has no entry in {index_rel}", rec.line,
            ))


def _check_supersession(records: list[_Record], findings: list[Finding]) -> None:
    by_id = {r.id: r for r in records}
    for rec in records:
        target = rec.obj.get("supersedes")
        if target is None:
            continue
        prior = by_id.get(target)
        if prior is None:
            findings.append(Finding(
                rec.path, f"record {rec.id!r} supersedes {target!r}, which does not exist "
                          f"anywhere in the store", rec.line,
            ))
        elif prior.obj.get("status") == ACTIVE_STATUS:
            findings.append(Finding(
                rec.path, f"record {rec.id!r} supersedes {target!r}, but {target!r} is still "
                          f"marked active at {prior.path}:{prior.line} instead of retired", rec.line,
            ))


def validate(repo_path: str = ".") -> list[Finding]:
    """Validate `<repo_path>/.mem`. Pure and read-only; returns every finding, worst first."""
    repo = Path(repo_path).resolve()
    mem = repo / ".mem"
    findings: list[Finding] = []
    if not mem.is_dir():
        return findings

    index_path = mem / INDEX_FILE
    domains_dir = mem / DOMAINS_DIR

    index_entries: list[tuple[int, dict]] = []
    if index_path.exists():
        index_rel = _relpath(repo, index_path)
        for lineno, raw, obj in _read_jsonl(index_path, repo, findings):
            _check_index_entry(index_rel, lineno, raw, obj, findings)
            index_entries.append((lineno, obj))
    elif domains_dir.is_dir() and any(domains_dir.glob("*.jsonl")):
        findings.append(Finding(_relpath(repo, mem), f"{DOMAINS_DIR}/ exists but {INDEX_FILE} is missing"))

    live_records = _collect_records(mem, repo, DOMAINS_DIR, retired=False, findings=findings)
    archived_records = _collect_records(mem, repo, ARCHIVE_DIR, retired=True, findings=findings)
    all_records = live_records + archived_records

    _check_unique_ids(all_records, findings)
    if index_path.exists():
        _check_index_consistency(index_entries, live_records, _relpath(repo, index_path), findings)
    _check_supersession(all_records, findings)

    return findings


def report(repo_path: str) -> bool:
    """Print every finding. Returns whether the store is valid."""
    findings = validate(repo_path)
    if not findings:
        print(f"memory OK ({repo_path})")
        return True
    print(f"memory INVALID ({repo_path}) — {len(findings)} finding(s):")
    for finding in findings:
        print(f"  {finding}")
    return False


def _main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] != "validate":
        print("usage: python -m control.memory validate [repo-path]", file=sys.stderr)
        return 2
    repo_path = argv[2] if len(argv) > 2 else "."
    return 0 if report(repo_path) else 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
