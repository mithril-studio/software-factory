"""The two dispatch scripts the factory runs inside a VM, and the prelude they share.

`VM_SCRIPT` and `REVIEW_SCRIPT` used to carry their own copy of the same git setup. The
duplication is the bug this file pins down: every step in the snapshot-goldens backlog edits
that setup, and two copies is how a build VM and a review VM quietly stop agreeing about what
they are looking at. So the checks below are about *shape*, not about behaviour of any one
line: one prelude, present in both scripts, at the front of both, exactly once.

The prelude also brings the repo now. A golden carried exactly one checkout for as long as a
golden was a project image; it is an *agent* image, so which repo a run works on is a property
of the run, and the prelude either finds that checkout or clones it. Getting that wrong is not
a crash — it is a run that quietly commits to whatever repo the machine happened to be holding.
So the clone logic is *executed* here, against stub `git` and `gh` commands on PATH, rather
than pattern-matched: what matters is which branch it takes and where it ends up.

The rest is the safety net for a change that must not alter behaviour. The exit codes are the
control plane's whole vocabulary for "the VM refused before the agent ever started" — `runner`
maps 90/91/92/93 onto human sentences — so a code that goes missing turns a precise failure
into an unexplained one. And both scripts are fed to `/bin/sh` on a machine we cannot attach a
debugger to, so they are parsed here instead.

Run it directly, no framework needed:

    .venv/bin/python -m control.vm_script_test
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from control import runner
from control.config import Settings
from control.runner import NODE_GUARD, PRELUDE, REVIEW_SCRIPT, VM_SCRIPT, dispatch_env

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

check("the prelude carries the cd into the checkout", 'cd "$dir"' in PRELUDE)
check("the prelude carries the clone", "gh repo clone" in PRELUDE)
check("the prelude carries safe.directory", "safe.directory" in PRELUDE)
check("the prelude carries the git identity", PRELUDE.count("git config user.") == 2)
check("the prelude carries the fetch", "git fetch --prune origin" in PRELUDE)

for name, script in SCRIPTS.items():
    check(f"{name}: starts with the shared prelude", script.startswith(PRELUDE))
    check(f"{name}: contains the prelude exactly once", script.count(PRELUDE), 1)
    # The point of the seam: nothing the prelude owns may be repeated in a tail, or the tail
    # is free to drift back into a second copy of it.
    check(f"{name}: fetches once", script.count("git fetch --prune origin"), 1)
    check(f"{name}: enters the checkout once", script.count('cd "$dir"'), 1)
    check(f"{name}: clones at most once", script.count("gh repo clone"), 1)
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

# ---------- clone or reuse
# Run, not read. The prelude decides where a run works and whether it clones, and both
# answers are invisible to pattern-matching: a script that clones the right repo into the
# wrong directory reads the same as one that gets it right. `git` and `gh` are stubs on
# PATH that record what they were asked, so the branch actually taken is what is checked.

STUB_GIT = """#!/bin/sh
echo "git $*" >> "$STUB_LOG"
[ "$3" = "remote" ] || exit 0
# `git -C <dir> remote get-url origin` — answered from a file the case wrote, so a checkout
# can be made to hold some *other* repo.
[ -f "$2/.origin" ] && cat "$2/.origin" && exit 0
exit 1
"""

STUB_GH = """#!/bin/sh
echo "gh $*" >> "$STUB_LOG"
[ -n "$STUB_GH_FAIL" ] && exit 1
mkdir -p "$4/.git" || exit 1
exit 0
"""

REPO = "acme/api"


def prelude(prepare=None, repo=REPO, **env):
    """Run PRELUDE in a throwaway HOME. Returns (exit code, output, what git and gh saw)."""
    home = Path(tempfile.mkdtemp())
    binaries = home / "bin"
    binaries.mkdir()
    for name, body in (("git", STUB_GIT), ("gh", STUB_GH)):
        path = binaries / name
        path.write_text(body)
        path.chmod(0o755)
    if prepare:
        prepare(home)
    log = home / "calls"
    environment = {
        "PATH": f"{binaries}:{os.environ['PATH']}",
        "HOME": str(home),
        "STUB_LOG": str(log),
        **({"FACTORY_REPO": repo} if repo is not None else {}),
        **env,
    }
    done = subprocess.run(["sh", "-c", PRELUDE], env=environment, capture_output=True, text=True)
    calls = log.read_text() if log.exists() else ""
    shutil.rmtree(home, ignore_errors=True)
    return done.returncode, done.stdout + done.stderr, calls


def checkout(path: Path, origin: str | None = None) -> None:
    """Make `path` look like a checkout, optionally of a particular repo."""
    (path / ".git").mkdir(parents=True)
    if origin:
        (path / ".origin").write_text(f"https://github.com/{origin}.git\n")


# Cold: nothing on the machine, so the run brings the repo itself.
code, out, calls = prelude()
check("clone or reuse: a machine with no checkout clones", "gh repo clone acme/api" in calls)
check("clone or reuse: into a directory named after the repo",
      [ln for ln in calls.splitlines() if ln.startswith("gh ")][0].split()[4].endswith("/work/api"))
check("clone or reuse: blobs are fetched lazily, history is not truncated",
      ("--filter=blob:none" in calls, "--depth" in calls), (True, False))
check("clone or reuse: and the clone is announced", "cloning acme/api" in out)
check("clone or reuse: the run then works in the clone", "git fetch --prune origin" in calls)
check("clone or reuse: a cold start still succeeds", code, 0)

# Warm: the snapshot already holds the repo, so the same script skips the clone. That is
# the entire difference between the warm tier and the cold one.
code, out, calls = prelude(prepare=lambda home: checkout(home / "work" / "api"))
check("clone or reuse: an existing checkout is reused", "gh repo clone" in calls, False)
check("clone or reuse: and the reuse is announced", "reusing the checkout" in out)
check("clone or reuse: a warm start succeeds", code, 0)

# The work directory is configurable, because $HOME/work is the VM's convention and not a
# promise the control plane makes.
code, out, calls = prelude(FACTORY_WORKDIR="/tmp/factory-test-work")
check("clone or reuse: FACTORY_WORKDIR decides where checkouts live",
      "/tmp/factory-test-work/api" in calls)

# The pre-clone override: honoured, but only for a checkout of the assigned repo. Anything
# else falls through to the clone, because working in a directory that holds some other
# repo is a run that pushes one repo's branch into another.
for name, origin, cloned in (
    ("holding the assigned repo", REPO, False),
    ("holding a different repo", "acme/other", True),
    ("holding no repo at all", None, True),
):
    tmp = Path(tempfile.mkdtemp())
    legacy = tmp / "legacy"
    if origin is None:
        legacy.mkdir()          # a directory, but not a checkout
    else:
        checkout(legacy, origin=origin)
    code, out, calls = prelude(FACTORY_REPO_DIR=str(legacy))
    check(f"clone or reuse: a pre-clone checkout {name} clones={cloned}",
          "gh repo clone" in calls, cloned)
    if not cloned:
        check("clone or reuse: and the pre-clone checkout is the one entered",
              str(legacy) in out)
    shutil.rmtree(tmp, ignore_errors=True)


# ---------- no repo
# A run with no repo has nowhere legitimate to work. The failure has to happen here: the
# alternative is an agent committing to whatever repo the machine was already holding.

for label, repo in (("unset", None), ("empty", "")):
    code, out, calls = prelude(repo=repo)
    check(f"no repo: {label} fails the run", code, 90)
    check(f"no repo: {label} says so", "no repo assigned" in out)
    check(f"no repo: {label} touches nothing first", calls, "")

# The other two ways a workspace can be unusable share the exit code, because they are the
# same fact from the machine's side: this run has nowhere to work.
code, out, calls = prelude(STUB_GH_FAIL="1")
check("no repo: a failed clone is exit 90", code, 90)
check("no repo: a failed clone says which repo", "clone of acme/api failed" in out)

blocker = Path(tempfile.mkdtemp()) / "a-file"
blocker.write_text("not a directory")
code, out, calls = prelude(FACTORY_WORKDIR=str(blocker / "work"))
check("no repo: a work directory that cannot be created is exit 90", code, 90)
shutil.rmtree(blocker.parent, ignore_errors=True)


# ---------- toolchain
# The guard used to say "the golden needs rebuilding", which was true while a golden carried
# one repo and could be pre-matched to its pin. An agent image serves every watched repo and
# they do not agree on a Node version, so a mismatch is now ordinary and gets repaired. Only
# one that survives the repair is fatal — and a guard that refuses a run it could have fixed
# is the same outage as one that lets a mismatch through, just louder.

STUB_NODE = """#!/bin/sh
echo "v$(cat "$STUB_NODE_STATE").0.0"
"""

STUB_FNM = """#!/bin/sh
[ "$1" = "use" ] && echo "$STUB_FNM_INSTALLS" > "$STUB_NODE_STATE"
exit 0
"""


def guard(pin="22", running="20", fnm=None):
    """Run NODE_GUARD in a checkout pinning `pin` on a machine running node `running`."""
    home = Path(tempfile.mkdtemp())
    binaries = home / "bin"
    binaries.mkdir()
    state = home / "node-version"
    state.write_text(running)
    stubs = {"node": STUB_NODE}
    if fnm is not None:
        stubs["fnm"] = STUB_FNM
    for name, body in stubs.items():
        path = binaries / name
        path.write_text(body)
        path.chmod(0o755)
    if pin:
        (home / ".nvmrc").write_text(f"{pin}\n")
    done = subprocess.run(
        ["sh", "-c", NODE_GUARD], cwd=home, capture_output=True, text=True,
        env={"PATH": f"{binaries}:{os.environ['PATH']}", "HOME": str(home),
             "STUB_NODE_STATE": str(state), "STUB_FNM_INSTALLS": fnm or ""},
    )
    shutil.rmtree(home, ignore_errors=True)
    return done.returncode, done.stdout + done.stderr


code, out = guard(pin="22", running="22")
check("toolchain: a machine that already matches is left alone", (code, out.strip()), (0, ""))

code, out = guard(pin="", running="20")
check("toolchain: a repo that pins nothing is left alone", (code, out.strip()), (0, ""))

code, out = guard(pin="22", running="20", fnm="22")
check("toolchain: a mismatch fnm can repair is repaired", code, 0)
check("toolchain: and the repair is announced", "installing it" in out)

code, out = guard(pin="22", running="20", fnm="21")
check("toolchain: a repair that does not take is still fatal", code, 93)
check("toolchain: and it no longer blames the golden",
      ("could not be repaired" in out, "needs rebuilding" in out), (True, False))

code, out = guard(pin="22", running="20")
check("toolchain: a machine with no fnm refuses rather than guessing", code, 93)


# ---------- env
# Both dispatch paths are built by one function, because the two must agree: a review VM
# that clones a different repo, or authenticates as somebody else, than the build VM whose
# work it is checking is not reviewing that work.

CONFIGURED = Settings(github_token="gho_test", workdir="/srv/work", repo_dir="/legacy/repo")


def envs(**overrides):
    """The build and review environments, under a known configuration."""
    real = runner.settings
    configured = dict(github_token=CONFIGURED.github_token, workdir=CONFIGURED.workdir,
                      repo_dir=CONFIGURED.repo_dir)
    runner.settings = Settings(**{**configured, **overrides})
    try:
        common = dict(repo=REPO, branch="factory/issue-7", base="main", prompt="do the thing",
                      run_id="run-1", number=7, vm_name="run-abc")
        return (dispatch_env(attempt=2, **common), dispatch_env(kind="review", **common))
    finally:
        runner.settings = real


build, review = envs()

for name, env in (("build", build), ("review", review)):
    check(f"env: {name} names the repo the run is for", env.get("FACTORY_REPO"), REPO)
    check(f"env: {name} carries a GitHub token", env.get("GH_TOKEN"), "gho_test")
    check(f"env: {name} names the work directory", env.get("FACTORY_WORKDIR"), "/srv/work")
    check(f"env: {name} still carries the pre-clone override", env.get("FACTORY_REPO_DIR"),
          "/legacy/repo")
    check(f"env: {name} carries the branch and the base", (env.get("FACTORY_BRANCH"),
          env.get("FACTORY_BASE")), ("factory/issue-7", "main"))
    check(f"env: {name} carries the prompt", env.get("FACTORY_PROMPT"), "do the thing")

# What the two paths may differ on, and nothing else.
check("env: the two paths differ only in the attempt and the trace",
      sorted(k for k in set(build) | set(review) if build.get(k) != review.get(k)),
      ["FACTORY_ATTEMPT", "OTEL_RESOURCE_ATTRIBUTES"])
check("env: only a build run counts attempts",
      ("FACTORY_ATTEMPT" in build, "FACTORY_ATTEMPT" in review), (True, False))
check("env: only a review run says so in its trace",
      ["kind=review" in e["OTEL_RESOURCE_ATTRIBUTES"] for e in (build, review)], [False, True])
check("env: the trace correlates the run with its issue and VM",
      build["OTEL_RESOURCE_ATTRIBUTES"],
      "run.id=run-1,issue=acme/api#7,repo=acme/api,vm=run-abc")

# An unset token must not be exported as an empty one: `gh` would take it and stop falling
# back to the golden's own login, turning "no token configured" into "authentication failed".
bare_build, bare_review = envs(github_token="")
check("env: no configured token exports none",
      ["GH_TOKEN" in e for e in (bare_build, bare_review)], [False, False])


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
