"""The two shell scripts that build and refresh a golden, checked without running either.

Neither can be executed here — they need boxd credentials and spend real machines — so what
is testable is the part that rots silently: the places where a shell script has to agree,
character for character, with Python it never imports.

Two such places, and both fail the same quiet way. `refresh-golden.sh` computes a repo slug
in `sed` to name the snapshot `golden-<slug>`; `control/agents.py` computes it in `re` to
decide which snapshot a run boots. Disagree by one character and the refresh updates a
snapshot no dispatch will ever resolve onto — no error anywhere, just a warm golden nobody
uses, and every run for that repo quietly paying the cold path instead.

And `refresh-golden.sh` reads the install command out of a repo's `## Setup` section in
`awk` and `sed`, while `preflight.setup_command` reads it in `re` for the control plane's own
provisioning. Disagree, and the same repo is installed two different ways depending on which
one warmed its golden — with nothing anywhere reporting that they differed.

Run it directly, no framework needed:

    .venv/bin/python -m control.golden_scripts_test
"""
import subprocess
import sys
from pathlib import Path

from control.agents import slug
from control.preflight import SETUP_SECTION, setup_command

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
# The base image is `golden-copy`, and a repo that could produce that name would overwrite the
# one image every unprovisioned repo falls back to. It cannot: `owner/name` always carries a
# `/`, which always becomes a hyphen, so no slug is ever a single bare word.
check("slug: no repo the script slugs could ever name the base image",
      any("golden-" + shell_slug(r) == "golden-copy" for r in REPOS), False)


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


# ---------- and they pull the same command out of it
# The heading check above says which lines *start* a setup section; this says what the two
# implementations then read from it. `provision.py` runs the command the Python half returns,
# `refresh-golden.sh` runs the one the shell half returns, and a repo warmed by either has to
# end up installed the same way.
SETUP_BLOCK = REFRESH.read_text().splitlines()
_start = next(i for i, line in enumerate(SETUP_BLOCK) if line.strip().startswith("setup=$(printf"))
_end = next(i for i in range(_start, len(SETUP_BLOCK)) if "head -1)" in SETUP_BLOCK[i])
SETUP_EXTRACT = "\n".join(SETUP_BLOCK[_start:_end + 1])


def shell_setup(profile: str) -> str:
    """Run the script's own extraction over a profile, with `$profile` fed from stdin."""
    program = "\n".join(['profile=$(cat)', SETUP_EXTRACT, 'printf %s "$setup"'])
    out = subprocess.run(
        ["/bin/sh", "-c", program], input=profile, capture_output=True, text=True, check=True
    )
    return out.stdout


PROFILES = [
    # The ordinary shape, and the one this repo's own .factory.md has.
    "# Repo\n\n## Setup\n\nRun `uv venv && uv pip install -e \".[dev]\"` first.\n\n## Verify\n\n`ruff check`\n",
    # The command is the first backticked line under the heading, not the first anywhere.
    "## Overview\n\n`npm test`\n\n## Setup\n\n`npm ci`\n",
    # Prose before the command, which is how most repos write it.
    "## Setup\n\nThis project uses pnpm.\n\nInstall with `pnpm install --frozen-lockfile`.\n",
    # The section ends at the next heading, whatever level.
    "## Setup\n\nnothing to install\n\n### Tests\n\n`make test`\n",
    # Two spans on a line: both take the last, because `sed`'s greedy `.*` does.
    "## Setup\n\nNot `this one` but `that one`\n",
    # Heading spellings the section check already accepts.
    "# set-up\n\n`make bootstrap`\n",
    "###### SETUP\n\n`cargo build`\n",
    # Nothing to find.
    "## Setup\n\nno command here\n",
    "## Verify\n\n`ruff check`\n",
    "",
]
for profile in PROFILES:
    first = profile.splitlines()[0] if profile else "(empty)"
    check(f"setup command: the script and preflight extract the same one from {first!r}",
          shell_setup(profile), setup_command(profile))

# This repo's own profile, since it is what the factory reads when warming its own golden.
OWN = (ROOT / ".factory.md").read_text()
check("setup command: and agree on this repo's own .factory.md",
      shell_setup(OWN), setup_command(OWN))
check("setup command: which names one at all", bool(setup_command(OWN)))


print()
print(f"{len(fails)} failed" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
