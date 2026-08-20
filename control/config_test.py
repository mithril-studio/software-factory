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


print()
if fails:
    print(f"{len(fails)} failed: " + ", ".join(fails))
    sys.exit(1)
print("all passed")
