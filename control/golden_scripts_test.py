"""The two shell scripts that build and refresh a golden, checked without running either.

Neither can be executed here — they need boxd credentials and spend real machines — so what
is testable is the part that rots silently: the places where a shell script has to agree,
character for character, with Python it never imports.

Two such places, and both fail the same quiet way. `refresh-golden.sh` computes a repo slug
in `sed` to name the snapshot `golden-<agent>--<slug>`; `control/agents.py` computes it in
`re` to decide which snapshot a run forks. Disagree by one character and the refresh updates
a snapshot no dispatch will ever resolve onto — no error anywhere, just a warm golden nobody
uses. And `refresh-golden.sh` reads the install command out of a repo's `## Setup` section in
`awk` while `preflight` warns about a missing one in `re`: disagree, and preflight calls a
repo ready that the refresh then refuses.

Run it directly, no framework needed:

    .venv/bin/python -m control.golden_scripts_test
"""
import subprocess
import sys
from pathlib import Path

from control.agents import slug
from control.preflight import SETUP_SECTION

fails = []


def check(name, got, want=True):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n       got={got!r} want={want!r}"))
    if not ok:
        fails.append(name)


ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "scripts" / "build-golden.sh"
REFRESH = ROOT / "scripts" / "refresh-golden.sh"


# ---------- both parse as shell
# `sh -n` reads the whole script and reports syntax errors without running a command, calling
# boxd, or needing an argument. The same check the acceptance criterion runs.
for path in (BUILD, REFRESH):
    check(f"{path.name} exists and is executable", path.is_file() and path.stat().st_mode & 0o111 != 0)
    done = subprocess.run(["sh", "-n", str(path)], capture_output=True, text=True, check=False)
    check(f"{path.name} parses as shell", (done.returncode, done.stderr.strip()), (0, ""))


# ---------- the slug is the same slug
# Lifted out of the script rather than retyped, so this cannot pass against a copy that has
# drifted from the one that actually runs.
SLUG_LINE = next(
    line for line in REFRESH.read_text().splitlines() if line.startswith("slug=")
)


def shell_slug(repo: str) -> str:
    """Run the script's own `slug=` line against one repo.

    Lifted out of the file and executed rather than reimplemented here, because a
    reimplementation is a second copy, and a second copy is the thing that drifts.
    """
    program = "\n".join(["repo=$1", SLUG_LINE, 'printf %s "$slug"'])
    out = subprocess.run(
        ["/bin/sh", "-c", program, "sh", repo], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


REPOS = [
    "mithril-studio/software-factory",
    "Mithril-Studio/Software-Factory",
    "acme/my_api.v2",
    "owner/--weird--name--",
    "UPPER-CASE/Repo_Name",
    "a-b-c/d-e-f",
    "a/app",
]
for repo in REPOS:
    check(f"slug: the script and agents.py agree on {repo}", shell_slug(repo), slug(repo))
check("slug: and never produces the separator that would split the name back wrong",
      any("--" in shell_slug(r) for r in REPOS), False)


# ---------- the same headings count as a setup section
# Only the pattern, not the action: the action is what the script does with a setup section,
# and what has to agree with preflight is which lines are one.
SETUP_PATTERN = next(
    line.strip().split("{", 1)[0].strip()
    for line in REFRESH.read_text().splitlines()
    if line.strip().startswith("/^#+")
)


def shell_finds_setup(text: str) -> bool:
    out = subprocess.run(
        ["/bin/sh", "-c", "awk '" + SETUP_PATTERN + ' { print "HIT"; exit }' + "'"],
        input=text, capture_output=True, text=True, check=True,
    )
    return "HIT" in out.stdout


HEADINGS = [
    ("## Setup", True),
    ("# setup", True),
    ("### set-up", True),
    ("###### SETUP", True),
    ("## Set up", True),
    ("## Verify", False),
    ("- run the setup command yourself", False),
    ("setup", False),
]
for line, expected in HEADINGS:
    both = (shell_finds_setup(line + "\n"), bool(SETUP_SECTION.search(line + "\n")))
    check(f"setup section: {line!r} reads the same to the script and to preflight",
          both, (expected, expected))


print()
print(f"{len(fails)} failed" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
