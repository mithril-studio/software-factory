#!/usr/bin/env bash
# Every gate CI runs on a pull request, in CI's order.
#
# It exists because the list drifted. `.github/workflows/ci.yml` grew a third gate — the
# repository-memory validator, added in #65 — and `.factory.md` went on telling agents that
# there were two and that those were "the only ones". Two of the four issues in the #49-54
# batch then failed CI on the gate nobody had been told to run, each costing a build VM and a
# review run to fix something that takes a second locally.
#
# So there is now one list, in one file, and both callers read it: CI runs this script, and
# `.factory.md` tells the agent to run this script. A gate added here reaches both by existing.
#
# Fails on the first gate that fails, like CI does. Run it from the repo root.
set -euo pipefail

cd "$(dirname "$0")/.."

# The agent installs into `.venv` and calls it directly, because each of its commands runs in
# its own shell and an activated environment does not survive that. CI pip-installs into the
# runner's own Python and has no `.venv` at all. Both are correct; this picks whichever is there.
if [ -x .venv/bin/python ]; then
  PY=.venv/bin/python
  RUFF=.venv/bin/ruff
else
  PY=python
  RUFF=ruff
fi

echo "== ruff (undefined names, unused imports, syntax)"
# Not `ruff check` in full: that reports pre-existing style findings, so it is red on arrival
# and would teach everyone to ignore it. Widen the selection when the backlog is clean.
"$RUFF" check --select F,E9 control telemetry

echo
echo "== tests"
# Every *_test.py in the repo, run as a module the way its own docstring says to. A new test
# file is picked up by existing here, not by being added to a list.
for f in control/*_test.py telemetry/*_test.py; do
  m=$(echo "${f%.py}" | tr / .)
  echo "-- $m"
  "$PY" -m "$m"
done

echo
echo "== repository memory"
# This repo's own .mem/ is checked-in context for future runs; a malformed or unscoped record
# in it must fail the same way it would in any other repo.
"$PY" -m control.memory validate .

echo
echo "ALL GATES PASSED"
