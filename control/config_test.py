"""What FACTORY_REPOS means, and what the control plane does with it.

An entry is a repo and nothing else now. It used to be `owner/repo=agent`, because the agent
was the first half of every golden's name and so had to be chosen before a snapshot could be
found. Goldens are named for the repo, so there is nothing left for that half to select —
which is why the checks below insist a legacy `=agent` suffix is *dropped* rather than
rejected: an existing deployment's `.env` must keep working across the upgrade instead of
silently watching nothing.

Two failure modes are kept apart on purpose, and most of the checks below are really about
that line. A *missing setting* is something nobody filled in, and it blocks starting a run.
A *problem* is a complete configuration with nothing to run on — no base image built yet —
which is a thing to go build, not a broken setting, and which can only be answered by asking
the fleet.

Nothing here reads the fleet, a database or a clock. Run it directly, no framework needed:

    .venv/bin/python -m control.config_test
"""
import os
import sys

from control.agents import BASE_SNAPSHOT
from control.config import Settings, _repos

fails = []


def check(name, got, want=True):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}"
          + ("" if ok else f"\n       got={got!r} want={want!r}"))
    if not ok:
        fails.append(name)


def repos(raw):
    """`_repos()` reads the environment when it is called, so set it and call it."""
    os.environ["FACTORY_REPOS"] = raw
    try:
        return _repos()
    finally:
        del os.environ["FACTORY_REPOS"]


def settings(raw):
    return Settings(repos=repos(raw))


# The snapshots a fleet might hold. Only the name matters — that is the whole registry.
WARM = "golden-acme-api"


# ---------- parsing

check("repos: a bare repo is the whole entry", repos("acme/api"), ("acme/api",))
check("repos: several entries, order preserved",
      repos("acme/api, acme/web , acme/ops"), ("acme/api", "acme/web", "acme/ops"))
check("repos: whitespace around a name is not part of it", repos("  acme/api  "), ("acme/api",))
check("repos: empty entries and a trailing comma are skipped",
      repos("acme/api,,acme/web,"), ("acme/api", "acme/web"))
check("repos: nothing configured is not an error", repos(""), ())

# The upgrade path. A deployment whose `.env` still carries the old syntax watches its repos,
# rather than parsing `acme/api=pi` as a repo name that matches nothing on GitHub.
check("repos: a legacy =agent suffix is dropped, not rejected",
      repos("acme/api=pi"), ("acme/api",))
check("repos: mixed old and new entries all survive",
      repos("acme/api=pi,acme/web,acme/ops=codex"), ("acme/api", "acme/web", "acme/ops"))
check("repos: and the agent half is gone rather than kept somewhere",
      settings("acme/api=pi").repos, ("acme/api",))


# ---------- missing: static gaps only, so it can gate a run without an await

full = Settings(boxd_api_key="k", github_token="t", repos=repos("acme/api"))
check("missing: nothing static is absent", full.missing(), [])
check("missing: the two settings nobody can guess",
      Settings(boxd_api_key="", github_token="", repos=()).missing(),
      ["BOXD_API_KEY", "GITHUB_TOKEN (or `gh auth login`)"])


# ---------- problems

# One question, because there is one image to be missing. Everything else the fleet might
# lack is a speed-up, and reporting a speed-up as a problem is how a page full of warnings
# stops being read.
NO_BASE = [
    f"no {BASE_SNAPSHOT} snapshot — every run boots it unless the repo has a warm golden of "
    "its own, so nothing can dispatch without it"
]
check("problems: no base image is the one thing that stops every repo",
      settings("acme/api").problems([]), NO_BASE)
check("problems: a warm golden alone is not enough — the base is what unprovisioned repos get",
      settings("acme/api").problems([WARM]), NO_BASE)
check("problems: with the base present there is nothing to report",
      settings("acme/api").problems([BASE_SNAPSHOT]), [])
check("problems: a repo with no golden of its own is not a problem, it is the ordinary case",
      settings("acme/api,acme/web").problems([BASE_SNAPSHOT, WARM]), [])
check("problems: watching nothing yet is not a problem either",
      settings("").problems([BASE_SNAPSHOT]), [])
check("problems: and it is never reported as a missing setting",
      Settings(boxd_api_key="k", github_token="t", repos=repos("acme/api")).missing(), [])


# ---------- the stall watchdog's threshold
#
# The one number in the system that can turn a working run into a false finding. An agent may
# hold a single Bash call open for `bash_max_timeout` while emitting nothing, so a watchdog at
# or below that reports a build in progress as a stall — the same class of error as a watch
# script reading a failed ssh as a frozen log. So the default is derived from that setting
# rather than written down beside it, and an override that undercuts it is *reported* rather
# than clamped: a threshold that silently moves cannot be reasoned about from the line it
# eventually prints.

check("idle: the default sits a turn's headroom above the longest allowed command",
      Settings(run_idle=0, bash_max_timeout=1800).idle_timeout(), 2700)
check("idle: it follows bash_max_timeout rather than a number of its own",
      Settings(run_idle=0, bash_max_timeout=600).idle_timeout(), 1500)
check("idle: an explicit setting is honoured",
      Settings(run_idle=3000, bash_max_timeout=1800).idle_timeout(), 3000)
check("idle: a watchdog below the command ceiling is a problem, not a clamp",
      Settings(run_idle=1200, bash_max_timeout=1800, repos=()).problems([BASE_SNAPSHOT]),
      ["FACTORY_RUN_IDLE=1200s is not above FACTORY_BASH_MAX_TIMEOUT=1800s — one legitimate "
       "command can be silent for longer than that, so runs will be failed as stalled while "
       "they are still working"])
check("idle: exactly equal is still too low — the ceiling is reachable",
      len(Settings(run_idle=1800, bash_max_timeout=1800, repos=()).problems([BASE_SNAPSHOT])), 1)
check("idle: above it, there is nothing to report",
      Settings(run_idle=2400, bash_max_timeout=1800, repos=()).problems([BASE_SNAPSHOT]), [])
check("idle: and the derived default never reports itself",
      Settings(run_idle=0, bash_max_timeout=1800, repos=()).problems([BASE_SNAPSHOT]), [])


print()
if fails:
    print(f"{len(fails)} failed: " + ", ".join(fails))
    sys.exit(1)
print("all passed")
