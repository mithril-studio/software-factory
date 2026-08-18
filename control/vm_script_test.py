"""The two dispatch scripts the factory runs inside a VM, and the prelude they share.

`VM_SCRIPT` and `REVIEW_SCRIPT` used to carry their own copy of the same git setup. The
duplication is the bug this file pins down: every step in the snapshot-goldens backlog edits
that setup, and two copies is how a build VM and a review VM quietly stop agreeing about what
they are looking at. So the checks below are about *shape*, not about behaviour of any one
line: one prelude, present in both scripts, at the front of both, exactly once.

The rest is the safety net for a change that must not alter behaviour. The exit codes are the
control plane's whole vocabulary for "the VM refused before the agent ever started" — `runner`
maps 90/91/92/93 onto human sentences — so a code that goes missing turns a precise failure
into an unexplained one. And both scripts are fed to `/bin/sh` on a machine we cannot attach a
debugger to, so they are parsed here instead.

Run it directly, no framework needed:

    .venv/bin/python -m control.vm_script_test
"""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from control.runner import NODE_GUARD, PRELUDE, REVIEW_SCRIPT, VM_SCRIPT

fails = []


def check(name, got, want=True):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n       got={got!r} want={want!r}"))
    if not ok:
        fails.append(name)


def order(script, *needles):
    """Where each needle sits in the script, or -1. Comparable, so order is checkable."""
    return [script.find(n) for n in needles]


SCRIPTS = {"build": VM_SCRIPT, "review": REVIEW_SCRIPT}

# ---------- one shared prelude

check("the prelude carries the cd into the checkout", 'cd "$FACTORY_REPO_DIR"' in PRELUDE)
check("the prelude carries safe.directory", "safe.directory" in PRELUDE)
check("the prelude carries the git identity", PRELUDE.count("git config user.") == 2)
check("the prelude carries the fetch", "git fetch --prune origin" in PRELUDE)

for name, script in SCRIPTS.items():
    check(f"{name}: starts with the shared prelude", script.startswith(PRELUDE))
    check(f"{name}: contains the prelude exactly once", script.count(PRELUDE), 1)
    # The point of the seam: nothing the prelude owns may be repeated in a tail, or the tail
    # is free to drift back into a second copy of it.
    check(f"{name}: fetches once", script.count("git fetch --prune origin"), 1)
    check(f"{name}: enters the checkout once", script.count('cd "$FACTORY_REPO_DIR"'), 1)
    check(f"{name}: asserts the toolchain once", script.count(NODE_GUARD), 1)

# The guard reads the pin out of the working tree, so it must stay behind each script's own
# checkout rather than move up into the prelude with the rest of the setup.
for name, script in SCRIPTS.items():
    cut, guard = order(script, "git checkout -B", NODE_GUARD)
    check(f"{name}: the toolchain assertion runs after the checkout", cut < guard)

# ---------- what each tail keeps to itself

check("only the build script resets from the base branch",
      ["origin/$FACTORY_BASE" in s for s in (VM_SCRIPT, REVIEW_SCRIPT)], [True, False])
check("only the build script resumes a previous attempt",
      ["$FACTORY_ATTEMPT" in s for s in (VM_SCRIPT, REVIEW_SCRIPT)], [True, False])
check("only the review script clears the stale verdict",
      ["rm -f /tmp/factory-verdict.json" in s for s in (VM_SCRIPT, REVIEW_SCRIPT)], [False, True])
check("the review script never pushes", "git push" in REVIEW_SCRIPT, False)

# The launch itself moved into the golden as `factory-agent`, so what stays checkable here is
# that each script still launches exactly one agent and does it last. The direct `claude -p`
# line survives only as the fallback for goldens captured before the wrapper existed, which is
# why it is now behind a `command -v` test rather than at the start of a line of its own.
# `control/manifest_test.py` owns the shape of that handoff.

for name, script in SCRIPTS.items():
    check(f"{name}: launches the agent exactly once", script.count("exec factory-agent"), 1)
    check(f"{name}: launches the agent last", script.rstrip().endswith("exec factory-agent"))
    check(f"{name}: keeps the pre-wrapper fallback exactly once",
          len(re.findall(r"claude -p ", script)), 1)

# ---------- exit codes
# 90 no repo dir · 91 fetch failed · 92 checkout failed · 93 toolchain mismatch. Each one is a
# sentence in the run log; losing one loses the sentence, not just the number.

for code in (90, 91, 92, 93):
    check(f"exit codes: the build script still exits {code}", f"exit {code}" in VM_SCRIPT)
    check(f"exit codes: the review script still exits {code}", f"exit {code}" in REVIEW_SCRIPT)

check("exit codes: no code escapes the agent's own range", sorted(set(re.findall(r"exit (\d+)", VM_SCRIPT))),
      ["90", "91", "92", "93"])

# ---------- posix shell
# Parsed, not run: `sh -n` reads the whole script and reports syntax errors without executing a
# command or needing a single FACTORY_* variable to be set.

for name, script in SCRIPTS.items():
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as fh:
        fh.write(script)
        path = fh.name
    done = subprocess.run(["sh", "-n", path], capture_output=True, text=True, check=False)
    Path(path).unlink()
    check(f"posix shell: the {name} script parses", (done.returncode, done.stderr.strip()), (0, ""))

print()
print(f"{len(fails)} failed" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
