"""Reading a probe's output, and deciding whether a repo may be dispatched to.

Both halves fail quietly if they are wrong: a probe key that stops being parsed reads as
"absent" rather than as an error, and a verdict that counts the wrong checks says READY about
a repo nothing can build.

The probe half outlived the probe. `preflight` stopped `exec`ing into goldens when they
became repo-agnostic snapshots with no checkout to inspect, but `probe.parse` is still how
any `key=value` round trip is read back, and the parsing bug it guards against — a key the
script never printed coming back as empty rather than absent — is silent in whichever caller
comes next.

Run it directly, no framework needed:

    .venv/bin/python -m control.preflight_test
"""
import sys

from control.preflight import Check, agent_check, profile_checks, report
from control.probe import parse

fails: list[str] = []


def check(name, got, want):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}\n       got={got!r}")
    if not ok:
        fails.append(name)


# ---------- the probe's output
PROBE = """repo_dir=ok
origin=mithril-studio/legal-ai-app
branch=main
head=fe128a9
dirty=0
fetch=ok
behind=0
skills=boxd memory
"""
p = parse(PROBE)
check("keys and values are split on the first =", p["origin"], "mithril-studio/legal-ai-app")
check("a value with spaces survives", p["skills"], "boxd memory")
check("a key the probe never printed is absent, not empty", p.get("gh"), None)
check("a line with no = is ignored", parse("noise\nk=v\n"), {"k": "v"})
check("an empty value stays empty rather than becoming a key", parse("behind=\n")["behind"], "")

# ---------- the verdict
ok_ = Check("a", True, "")
warn = Check("b", False, "", fatal=False)
bad = Check("c", False, "")
check("all clear -> ready", report("r", [ok_]), True)
check("a warning alone -> still ready", report("r", [ok_, warn]), True)
check("one blocking check -> not ready", report("r", [ok_, warn, bad]), False)
check("nothing checked -> ready is vacuous but honest", report("r", []), True)

# ---------- how each is shown
check("a blocking failure reads FAIL", bad.mark, "FAIL")
check("a non-blocking one reads warn", warn.mark, "warn")

# ---------- setup warning  (AC3)
# The profile's most valuable line is the one naming how to install the repo, because the
# prompt now sends the agent to run it before it touches any code. A repo that names none
# still builds — the agent works it out from the lock file — so this is a warning, and a
# preflight that blocked onboarding over it would be refusing a repo no run would fail on.
WITH_SETUP = "## Setup\n\n`npm ci`\n\n## Verify\n\n`npm test`\n"
NO_SETUP = "## Verify\n\n`npm test`\n"


def profile(text):
    return {c.name: c for c in profile_checks(text)}


named = profile(WITH_SETUP)
check("setup warning: a profile naming one is quiet",
      named[".factory.md names a setup command"].ok, True)
check("setup warning: and the profile itself counts as present",
      named["has .factory.md"].ok, True)

silent = profile(NO_SETUP)
check("setup warning: a profile naming none is reported",
      silent[".factory.md names a setup command"].ok, False)
check("setup warning: as a warning, never a blocking failure",
      silent[".factory.md names a setup command"].fatal, False)
check("setup warning: so the repo is still ready",
      report("r", profile_checks(NO_SETUP)), True)
check("setup warning: and it reads warn, not FAIL",
      silent[".factory.md names a setup command"].mark, "warn")

check("setup warning: any heading level names it, and the spelling is not the point",
      profile("### set-up\n`uv sync --frozen`\n")[".factory.md names a setup command"].ok, True)
check("setup warning: the word in a sentence is not a section",
      profile("- run the setup command yourself\n")[".factory.md names a setup command"].ok,
      False)

check("setup warning: no profile at all is a warning too, and only one",
      [(c.ok, c.fatal) for c in profile_checks(None)], [(False, False)])
check("setup warning: a whitespace-only profile is no profile",
      [(c.ok, c.fatal) for c in profile_checks("   \n")], [(False, False)])
check("setup warning: a missing profile is not also blamed for the setup line",
      any("setup" in c.name for c in profile_checks(None)), False)

# ---------- no agent  (AC3)
# The one machine-side question left, and the cheapest of the six the VM probe used to ask.
# Blocking, unlike the profile warnings: a repo whose agent resolves to no snapshot does not
# build worse, it cannot be dispatched at all — the fork has nothing to fork from.
REPO = "acme/api"
WARM = "golden-claude--acme-api"

check("no agent: a fleet with the agent's image is ready",
      agent_check(REPO, "claude", ["golden-claude"]).ok, True)
check("no agent: a warm snapshot answers for it too",
      agent_check(REPO, "claude", [WARM]).ok, True)
check("no agent: the detail names the snapshot that would boot",
      agent_check(REPO, "claude", [WARM]).detail, f"claude boots {WARM}")

missing = agent_check(REPO, "pi", ["golden-claude", WARM])
check("no agent: an agent no snapshot provides is not ready", missing.ok, False)
check("no agent: and it blocks, rather than warning", missing.fatal, True)
check("no agent: so the repo is reported not ready", report(REPO, [missing]), False)
check("no agent: the detail names the snapshot somebody has to build",
      "golden-pi" in missing.detail, True)
check("no agent: and what the fleet does hold, so the fix is obvious",
      WARM in missing.detail, True)

empty = agent_check(REPO, "claude", [])
check("no agent: an empty fleet is not ready either", empty.ok, False)
check("no agent: and says so rather than listing nothing",
      "no golden snapshots at all" in empty.detail, True)
check("no agent: another repo's warm golden is never borrowed",
      agent_check("acme/other", "claude", [WARM]).ok, False)

print()
print(f"{len(fails)} failed" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
