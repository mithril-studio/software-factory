"""Whether a dispatched script's environment reaches the processes the script starts.

On 2026-08-19 every dispatch died at exit 90 with `gh` reporting "please run gh auth login",
while the very same log line printed the repository name the prelude had just read out of
`$FACTORY_REPO`. Both facts were true at once, and together they name the bug precisely.

boxd's wire protocol carries no environment. Its SDK compensates by prefixing the command
with `K=v` assignments — right for a one-line command, wrong for a script: ahead of a
multi-line command the prefix becomes a line of bare assignments, which sets *shell*
variables. `$FACTORY_REPO` expands; `gh` inherits nothing.

The checks below are the two halves of that, run through a real `/bin/sh` rather than
asserted about a string, because the claim being made is about shell semantics and only a
shell can settle it. The first half reproduces the failure and would pass against the broken
build too — it is there so the second half cannot be read as proving something weaker than
it does.

Run it directly, no framework needed:

    .venv/bin/python -m control.dispatch_env_test
"""
import shlex
import subprocess
import sys

from control.runner import export_prelude

fails = []


def check(name, got, want=True):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n       got={got!r} want={want!r}"))
    if not ok:
        fails.append(name)


def sdk_prefix(env):
    """Exactly what `boxd.resources.machines._exec_init` does to the command."""
    return " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())


def run(env, script):
    """Dispatch `script` the way `_stream` does, and return what a child process saw."""
    command = f"{sdk_prefix(env)} {export_prelude(env) + script}"
    out = subprocess.run(["/bin/sh", "-c", command], capture_output=True, text=True, timeout=30)
    return out.stdout.strip()


ENV = {"FACTORY_REPO": "mithril-studio/software-factory", "GH_TOKEN": "t" * 93}

# VM_SCRIPT begins with a newline, so the SDK's assignments land on a line of their own.
# `$FACTORY_REPO` is read by the script; `$GH_TOKEN` is read by a child, as `gh` reads it.
SCRIPT = "\n" + 'echo "script=[$FACTORY_REPO]"; sh -c \'echo "child=${#GH_TOKEN}"\'\n'


# --- the half that reproduces the failure -----------------------------------------------
# Without the prelude the script still reads its own variables, which is why the logs looked
# like the environment had arrived.
broken = subprocess.run(
    ["/bin/sh", "-c", f"{sdk_prefix(ENV)} {SCRIPT}"], capture_output=True, text=True, timeout=30
).stdout.strip()
check("without the prelude: the script itself still sees the variable",
      "script=[mithril-studio/software-factory]" in broken)
check("without the prelude: a child process sees nothing — the 2026-08-19 failure",
      "child=0" in broken)


# --- the half that fixes it --------------------------------------------------------------
fixed = run(ENV, SCRIPT)
check("with the prelude: the script still sees the variable",
      "script=[mithril-studio/software-factory]" in fixed)
check("with the prelude: a child process inherits the token, whole",
      "child=93" in fixed)


# --- the prelude itself -------------------------------------------------------------------
check("leads with a newline, so the SDK's prefix stays a line of pure assignments",
      export_prelude({"A": "1"}).startswith("\n"))
check("ends with a newline, so the script's first line is not swallowed",
      export_prelude({"A": "1"}).endswith("\n"))
check("names every variable it was given",
      export_prelude(dict.fromkeys(["A", "B_2", "_C"], "x")).strip(), "export A B_2 _C")
check("no variables is no statement, not a bare `export` that lists the whole environment",
      export_prelude({}), "")

# A key that is not a shell name could only ever be pasted into the command as syntax.
check("a key that is not a shell name is dropped rather than emitted",
      export_prelude({"OK": "1", "not-a-name": "2", "9bad": "3", "sp ace": "4"}).strip(),
      "export OK")

# The real dispatch environment, so a variable added to dispatch_env is covered by this file
# rather than needing to be remembered here.
REAL = ["FACTORY_REPO", "FACTORY_BRANCH", "FACTORY_BASE", "FACTORY_PROMPT", "GH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN", "BASH_DEFAULT_TIMEOUT_MS", "OTEL_RESOURCE_ATTRIBUTES"]
emitted = export_prelude(dict.fromkeys(REAL, "x")).split()[1:]
check("every name dispatch_env sets is exported", sorted(emitted), sorted(REAL))

# The prompt is multi-line and full of quotes; the SDK quotes it, and the prelude must not
# care. A prompt that broke the command would fail the run before the agent started.
PROMPT = "line one\nline 'two' with \"quotes\" and $DOLLAR and `backticks`\n"
multiline = run({"FACTORY_PROMPT": PROMPT}, "\n" + 'sh -c \'printf "%s" "$FACTORY_PROMPT" | wc -l\'\n')
check("a multi-line, quote-laden prompt survives into a child process", multiline, "2")


print()
if fails:
    print(f"{len(fails)} failed: " + ", ".join(fails))
    sys.exit(1)
print("all passed")
