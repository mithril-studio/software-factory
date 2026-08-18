#!/usr/bin/env python3
"""Validate a drafted backlog before any issue is created.

Usage: validate_backlog.py <slug> <workdir>

<workdir> holds one file per issue, NN.md, in the format create_backlog.sh expects.

Why this exists: every defect in a drafted issue degrades *silently*. The control plane's
parser (control/runner.py:parse_criteria) drops anything it cannot read and never says so —
a YAML syntax error skips review for that PR entirely, and a single malformed criterion is
dropped while its siblings still run, so the reviewer approves against a fraction of the
contract. Nothing renders wrong on GitHub. The only place to catch it is before creation.

The criteria regex and the accepted modes are copied from the control plane deliberately:
this must fail on exactly what the control plane would silently discard, so keep them in
sync if that parser changes.

Exit 0 = safe to create. Exit 1 = do not create.
"""

import pathlib
import re
import sys

try:
    import yaml
except ImportError:
    sys.exit("validate_backlog: PyYAML is required (pip install pyyaml)")

# Must match control/runner.py exactly.
CRITERIA_BLOCK = re.compile(
    r"^##\s*Acceptance criteria\s*?\n+```ya?ml\n(.*?)^```", re.S | re.M | re.I
)
BLOCKING_MODES = ("test", "probe", "structure")
ALL_MODES = (*BLOCKING_MODES, "inspect")

REQUIRED_HEADINGS = ["## Objective", "## Task", "## Acceptance criteria", "## Boundaries",
                     "## Sequence"]

errors: list[str] = []
warnings: list[str] = []


def check(path: pathlib.Path, slug: str, step: int, total: int) -> None:
    where = path.name
    text = path.read_text()
    lines = text.splitlines()

    if not lines or not lines[0].startswith("TITLE:"):
        errors.append(f"{where}: first line must be 'TITLE: <title>'")
        return
    if not lines[0][len("TITLE:"):].strip():
        errors.append(f"{where}: TITLE is empty")
    body = "\n".join(lines[1:]).lstrip("\n")

    marker = f"<!-- factory-compose: {slug} step {step}/{total} -->"
    if not body.startswith(marker):
        errors.append(f"{where}: body must open with the marker {marker!r}")

    for heading in REQUIRED_HEADINGS:
        if not re.search(rf"^{re.escape(heading)}\s*$", body, re.M):
            errors.append(f"{where}: missing required section '{heading}'")

    if "Depends on: PENDING" not in body:
        errors.append(f"{where}: missing the literal token 'Depends on: PENDING' "
                      "(create_backlog.sh back-fills it)")

    match = CRITERIA_BLOCK.search(body)
    if not match:
        errors.append(f"{where}: no parseable '## Acceptance criteria' yaml block — the control "
                      "plane would skip review for this issue entirely")
        return
    try:
        parsed = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        first = str(exc).splitlines()[0]
        errors.append(f"{where}: acceptance criteria are not valid YAML ({first}). The whole "
                      "block would be dropped and the PR would merge ungated. Quote every "
                      "statement and verify.")
        return
    if not isinstance(parsed, list) or not parsed:
        errors.append(f"{where}: acceptance criteria must be a non-empty YAML list")
        return

    seen: set[str] = set()
    blocking = 0
    verifies: list[str] = []
    for i, item in enumerate(parsed, 1):
        tag = f"{where} criterion {i}"
        if not isinstance(item, dict):
            errors.append(f"{tag}: must be a mapping, got {type(item).__name__}")
            continue
        missing = {"id", "mode", "statement", "verify"} - set(item)
        if missing:
            errors.append(f"{tag}: missing {', '.join(sorted(missing))} — it would be dropped "
                          "silently and the reviewer would run the rest without it")
            continue
        if item["mode"] not in ALL_MODES:
            errors.append(f"{tag}: mode {item['mode']!r} is not one of {', '.join(ALL_MODES)} "
                          "— it would be dropped silently")
            continue
        if item["id"] in seen:
            errors.append(f"{tag}: duplicate id {item['id']!r}")
        seen.add(item["id"])
        if not str(item["statement"]).strip():
            errors.append(f"{tag}: statement is empty")
        if not str(item["verify"]).strip():
            errors.append(f"{tag}: verify is empty — nothing for the reviewer to run")
        if item["mode"] in BLOCKING_MODES:
            blocking += 1
        if item["mode"] == "test":
            verifies.append(str(item["verify"]).split("::")[0])

    if blocking == 0:
        errors.append(f"{where}: no criterion can block a merge (none is test/probe/structure, "
                      "or the ones that were got dropped above). This issue would be waved "
                      "through. Add at least one blocking criterion.")

    # Advisory: the file map and the test criteria should name the same files.
    file_map = re.search(r"^## Where this goes\s*$(.*?)^## ", body, re.S | re.M)
    if file_map:
        listed = file_map.group(1)
        for v in verifies:
            if v and v not in listed:
                warnings.append(f"{where}: test path {v!r} is not listed in '## Where this goes'")
    elif verifies:
        warnings.append(f"{where}: no '## Where this goes' section — the building agent will "
                        "have to work out file locations itself")


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: validate_backlog.py <slug> <workdir>", file=sys.stderr)
        return 1
    slug, workdir = sys.argv[1], pathlib.Path(sys.argv[2])
    files = sorted(p for p in workdir.glob("[0-9]*.md"))
    if not files:
        print(f"no NN.md step files found in {workdir}", file=sys.stderr)
        return 1

    for step, path in enumerate(files, 1):
        check(path, slug, step, len(files))

    for w in warnings:
        print(f"  warning: {w}")
    for e in errors:
        print(f"  ERROR:   {e}", file=sys.stderr)

    if errors:
        print(f"\n{len(errors)} problem(s) across {len(files)} issue(s). Nothing was created.",
              file=sys.stderr)
        return 1
    print(f"{len(files)} issue(s) valid"
          + (f", {len(warnings)} warning(s)" if warnings else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
