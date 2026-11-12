"""The script the factory runs inside a VM, executed against stub `git`, `gh` and agent.

The script decides which repo a run works in, which branch it starts from, and whether it
continues earlier work or throws a stale branch away. None of that is visible to pattern
matching — a script that checks out the wrong base reads almost the same as one that gets it
right — so the decisions are *run* here, with stubs on PATH recording what they were asked.

Exit codes 90-93 are the control plane's vocabulary for "the VM refused before the agent
started", so they are pinned too, and the script is parsed with `sh -n`.

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
from control.runner import NODE_GUARD, PRELUDE, VM_SCRIPT, dispatch_env

fails = []


def check(name, got, want=True):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n       got={got!r} want={want!r}"))
    if not ok:
        fails.append(name)


REPO = "acme/api"

# ---------- shape

check("the prelude carries the cd into the checkout", 'cd "$dir"' in PRELUDE)
check("the prelude carries the clone", "gh repo clone" in PRELUDE)
check("the prelude carries the fetch", "git fetch --prune --quiet origin" in PRELUDE)
check("the script starts with the prelude", VM_SCRIPT.startswith(PRELUDE))
check("the script fetches once", VM_SCRIPT.count("git fetch --prune --quiet origin"), 1)
check("the script asserts the toolchain once", VM_SCRIPT.count(NODE_GUARD), 1)
check("the toolchain assertion runs after the checkout",
      VM_SCRIPT.find("git checkout -B") < VM_SCRIPT.find(NODE_GUARD))
check("the agent is launched exactly once, and last",
      (VM_SCRIPT.count("exec factory-agent"), VM_SCRIPT.rstrip().endswith("exec factory-agent")),
      (1, True))
check("the prompt is never handed to another shell", "$FACTORY_PROMPT" in VM_SCRIPT, False)


# ---------- stubs

STUB_GIT = """#!/bin/sh
echo "git $*" >> "$STUB_LOG"
case "$1" in
  rev-parse) [ -n "$STUB_REMOTE_BRANCH" ] && exit 0 || exit 1 ;;
  push) [ -n "$STUB_PUSH_FAIL" ] && exit 1 || exit 0 ;;
esac
exit 0
"""

STUB_GH = """#!/bin/sh
echo "gh $*" >> "$STUB_LOG"
[ -n "$STUB_GH_FAIL" ] && exit 1
mkdir -p "$4/.git" || exit 1
exit 0
"""

STUB_AGENT = """#!/bin/sh
echo "agent ran on $FACTORY_BRANCH" >> "$STUB_LOG"
exit 0
"""


def run(script, prepare=None, repo=REPO, agent=True, **env):
    """Run `script` in a throwaway HOME. Returns (exit code, output, what the stubs saw)."""
    home = Path(tempfile.mkdtemp())
    binaries = home / "bin"
    binaries.mkdir()
    stubs = {"git": STUB_GIT, "gh": STUB_GH}
    if agent:
        stubs["factory-agent"] = STUB_AGENT
    for name, body in stubs.items():
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
    done = subprocess.run(["sh", "-c", script], env=environment, capture_output=True, text=True)
    calls = log.read_text() if log.exists() else ""
    shutil.rmtree(home, ignore_errors=True)
    return done.returncode, done.stdout + done.stderr, calls


def checkout(path: Path) -> None:
    (path / ".git").mkdir(parents=True)


# ---------- clone or reuse

code, out, calls = run(PRELUDE)
check("clone: a machine with no checkout clones", "gh repo clone acme/api" in calls)
check("clone: into a directory named after the repo",
      [ln for ln in calls.splitlines() if ln.startswith("gh ")][0].split()[4].endswith("/work/api"))
check("clone: blobs are fetched lazily, history is not truncated",
      ("--filter=blob:none" in calls, "--depth" in calls), (True, False))
check("clone: a cold start succeeds", code, 0)

code, out, calls = run(PRELUDE, prepare=lambda home: checkout(home / "work" / "api"))
check("reuse: an existing checkout is reused", "gh repo clone" in calls, False)
check("reuse: and the reuse is announced", "reusing the checkout" in out)

workdir = Path(tempfile.mkdtemp()) / "elsewhere"
code, out, calls = run(PRELUDE, FACTORY_WORKDIR=str(workdir))
check("workdir: FACTORY_WORKDIR decides where checkouts live", f"{workdir}/api" in calls)
shutil.rmtree(workdir.parent, ignore_errors=True)

# ---------- no repo

for label, repo in (("unset", None), ("empty", "")):
    code, out, calls = run(PRELUDE, repo=repo)
    check(f"no repo: {label} fails the run with 90", code, 90)
    check(f"no repo: {label} touches nothing first", calls, "")

code, out, calls = run(PRELUDE, STUB_GH_FAIL="1")
check("no repo: a failed clone is exit 90 and names the repo",
      (code, "clone of acme/api failed" in out), (90, True))


# ---------- which branch the run starts from
# The control plane sets FACTORY_RESUME from the PR history; the script only acts on it.

BRANCH = dict(FACTORY_BRANCH="factory/issue-7", FACTORY_BASE="test")

code, out, calls = run(VM_SCRIPT, FACTORY_RESUME="1", **BRANCH)
check("branch: no remote branch yet starts from the base",
      "git checkout -B factory/issue-7 origin/test" in calls)
check("branch: and the agent runs", ("agent ran on factory/issue-7" in calls, code), (True, 0))

code, out, calls = run(VM_SCRIPT, FACTORY_RESUME="1", STUB_REMOTE_BRANCH="1", **BRANCH)
check("branch: resume continues the remote branch",
      "git checkout -B factory/issue-7 origin/factory/issue-7" in calls)
check("branch: and does not delete it", "push origin --delete" in calls, False)

code, out, calls = run(VM_SCRIPT, FACTORY_RESUME="0", STUB_REMOTE_BRANCH="1", **BRANCH)
check("branch: a stale branch (closed PR) is deleted first",
      "git push origin --delete factory/issue-7" in calls)
check("branch: and the run starts fresh from the base",
      "git checkout -B factory/issue-7 origin/test" in calls)
check("branch: in that order", calls.find("--delete") < calls.find("checkout -B"))

code, out, calls = run(
    VM_SCRIPT, FACTORY_RESUME="0", STUB_REMOTE_BRANCH="1", STUB_PUSH_FAIL="1", **BRANCH
)
check("branch: a stale branch that cannot be deleted stops the run with 92", code, 92)
check("branch: before the agent starts", "agent ran" in calls, False)

code, out, calls = run(VM_SCRIPT, agent=False, FACTORY_RESUME="1", **BRANCH)
check("agent: a golden without factory-agent is exit 90", code, 90)
check("agent: and says so", "has no factory-agent" in out)


# ---------- toolchain

STUB_NODE = """#!/bin/sh
echo "v$(cat "$STUB_NODE_STATE").0.0"
"""

STUB_FNM = """#!/bin/sh
[ "$1" = "use" ] && echo "$STUB_FNM_INSTALLS" > "$STUB_NODE_STATE"
exit 0
"""


def guard(pin="22", running="20", fnm=None):
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


check("toolchain: a matching machine is left alone", guard("22", "22")[0], 0)
check("toolchain: a repo that pins nothing is left alone", guard("", "20")[0], 0)
code, out = guard("22", "20", fnm="22")
check("toolchain: a mismatch fnm can repair is repaired", (code, "installing it" in out), (0, True))
check("toolchain: a repair that does not take is fatal", guard("22", "20", fnm="21")[0], 93)
check("toolchain: no fnm refuses rather than guessing", guard("22", "20")[0], 93)


# ---------- env


def env(**overrides):
    real = runner.settings
    runner.settings = Settings(**{"github_token": "gho_test", "workdir": "/srv/work", **overrides})
    try:
        return dispatch_env(REPO, "factory/issue-7", "test", "do the thing", "run-1", 7,
                            "job-abc", resume=True)
    finally:
        runner.settings = real


e = env()
check("env: names the repo", e.get("FACTORY_REPO"), REPO)
check("env: carries the GitHub token", e.get("GH_TOKEN"), "gho_test")
check("env: names the work directory", e.get("FACTORY_WORKDIR"), "/srv/work")
check("env: carries the branch and the base",
      (e.get("FACTORY_BRANCH"), e.get("FACTORY_BASE")), ("factory/issue-7", "test"))
check("env: carries the prompt", e.get("FACTORY_PROMPT"), "do the thing")
check("env: carries the resume decision", e.get("FACTORY_RESUME"), "1")
check("env: the trace correlates run, issue and VM", e["OTEL_RESOURCE_ATTRIBUTES"],
      "run.id=run-1,issue=acme/api#7,repo=acme/api,vm=job-abc")
check("env: no configured token exports none", "GH_TOKEN" in env(github_token=""), False)
check("env: every name is exportable",
      all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k) for k in e))


# ---------- exit codes and syntax

check("exit codes: only the script's own range appears",
      sorted(set(re.findall(r"exit (\d+)", VM_SCRIPT))), ["90", "91", "92", "93"])

with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as fh:
    fh.write(VM_SCRIPT)
    path = fh.name
done = subprocess.run(["sh", "-n", path], capture_output=True, text=True, check=False)
Path(path).unlink()
check("posix shell: the script parses", (done.returncode, done.stderr.strip()), (0, ""))

print()
print(f"{len(fails)} failed" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
