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
import asyncio
import sys

import httpx

import control.github as gh
from control.preflight import Check, golden_check, profile_checks, push_check, report
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

# ---------- is there a golden to boot?
# The one machine-side question left, and the cheapest of the six the VM probe used to ask.
#
# It got narrower when the agent left the snapshot name. It used to fail for a repo whose
# *agent* had no image — a state a deployment could reach by typo. Now a repo with no golden
# of its own boots the base and installs for itself, so the only blocking answer is a fleet
# with no base image at all: not this repo's problem to fix, and one that stops every other
# repo too. Reporting an unprovisioned repo as not-ready would make connecting one wait on
# provisioning, which is exactly what the two-tier fallback exists to avoid.
REPO = "acme/api"
BASE = "golden-copy"
WARM = "golden-acme-api"

warm = golden_check(REPO, [BASE, WARM])
check("golden: a repo with its own warm snapshot is ready", warm.ok, True)
check("golden: and the detail names the snapshot that would boot", BASE in warm.detail, False)
check("golden: which is the warm one", WARM in warm.detail, True)

cold = golden_check(REPO, [BASE])
check("golden: a repo with no golden of its own is ready anyway", cold.ok, True)
check("golden: and it does not block", cold.fatal, True)
check("golden: so the repo is reported ready", report(REPO, [cold]), True)
check("golden: the detail says which tier answered", BASE in cold.detail, True)
check("golden: and says provisioning is a speed-up rather than a repair",
      "not a blocker" in cold.detail, True)

check("golden: another repo's warm golden is never borrowed, the base answers instead",
      golden_check("acme/other", [BASE, WARM]).detail.startswith(BASE), True)

# ---------- the four states behind one `ok`
#
# `scripts/factory-health.sh` renders every check here into a GitHub Actions report, so this
# detail line is where the control plane's own `no run has yet proved` warning finally reaches
# a human. It warned every five minutes for three hours; nothing read it, and two runs stalled
# on the golden it was about. None of these blocks: the warm tier is a speed-up, and a run
# that boots the base still builds.

FAILED = {WARM: {"last_verdict": "failed", "verdict_run": "r-d136841b"}}
PROVED = {WARM: {"last_verdict": "succeeded", "verified_at": "2026-08-20T09:00:00Z"}}

unproven = golden_check(REPO, [BASE, WARM], {})
check("golden: an unproven warm golden still boots", WARM in unproven.detail, True)
check("golden: and the detail says nothing has ever proved it",
      "no run has produced usage on it yet" in unproven.detail, True)

proved = golden_check(REPO, [BASE, WARM], PROVED)
check("golden: a proved one says when", "last proved 2026-08-20" in proved.detail, True)

skipped = golden_check(REPO, [BASE, WARM], FAILED)
check("golden: a warm golden with only a failure behind it is reported as skipped",
      skipped.detail.startswith(BASE), True)
check("golden: the detail names the run that did it, so the claim is checkable",
      "r-d136841b" in skipped.detail, True)
check("golden: and says the repo still builds, because it does", skipped.ok, True)
check("golden: which means the repo is still ready", report(REPO, [skipped]), True)
check("golden: with the repair being a re-warm, not an emergency",
      "re-warm" in skipped.detail, True)

# The difference this whole path exists to preserve: a golden nobody has run on and a ledger
# nobody could read are both "unknown", and neither may read as a bad image.
check("golden: no evidence at all reads the same as an unproven golden",
      golden_check(REPO, [BASE, WARM]).detail, unproven.detail)

# The one thing that really stops a dispatch. Asked of a repo with no warm golden of its own,
# because a repo that has one is the case where the base is not needed.
missing = golden_check("acme/other", [WARM])
check("golden: a fleet with no base image is not ready", missing.ok, False)
check("golden: and it blocks, rather than warning", missing.fatal, True)
check("golden: so the repo is reported not ready", report("acme/other", [missing]), False)
check("golden: the detail names the snapshot somebody has to build",
      BASE in missing.detail, True)
check("golden: and what the fleet does hold, so the fix is obvious",
      WARM in missing.detail, True)

empty = golden_check(REPO, [])
check("golden: an empty fleet is not ready either", empty.ok, False)
check("golden: and says so rather than listing nothing",
      "no golden snapshots at all" in empty.detail, True)


# ---------- can this token push?  (the check that used to answer without asking)
#
# `preflight` read `push` out of `GET /repos`'s permissions block, which describes the
# *account* and not the token. On a repo its owner's token was scoped read-only it printed
# `ok  token can push` — the same line, byte for byte, before and after the credential was
# replaced with one that could. It measured nothing. What replaces it asks git the question
# git will be asked at push time.


class _Advert:
    """The smart-HTTP advertisement `git push` opens with."""

    def __init__(self, status):
        self.status_code = status


def can_push(status, token="ghp_stub"):
    """Run `github.can_push` against a stubbed git host. `status` may be an exception."""
    seen: list = []

    async def get(self, url, **kw):
        seen.append((url, (kw.get("params") or {}).get("service")))
        if isinstance(status, Exception):
            raise status
        return _Advert(status)

    original_get, httpx.AsyncClient.get = httpx.AsyncClient.get, get
    original_settings, gh.settings = gh.settings, type("S", (), {"github_token": token})()
    try:
        return asyncio.run(gh.can_push("o/r")), seen
    finally:
        httpx.AsyncClient.get = original_get
        gh.settings = original_settings


(ok, detail), seen = can_push(200)
check("push: 200 from git-receive-pack means yes", ok, True)
check("push: it asks the git host, not the API host", seen[0][0], "https://github.com/o/r.git/info/refs")
check("push: and asks about receive-pack, the service a push uses", seen[0][1], "git-receive-pack")

(ok, detail), _ = can_push(403)
check("push: 403 means no", ok, False)
check("push: and says which way round it failed", "read" in detail and "not write" in detail, True)

(ok, _), _ = can_push(401)
check("push: 401 means no as well", ok, False)

# Anything else is neither a yes nor a no, and must not be read as a yes. The bug being
# fixed here is a check that reported ok without having established anything.
(ok, detail), _ = can_push(500)
check("push: an unexpected status is not a yes", ok, False)
check("push: and says so plainly", "neither yes nor no" in detail, True)

(ok, detail), calls = can_push(httpx.ConnectError("boom"))
check("push: an unreachable host is a finding, not a crash", ok, False)
check("push: which names the network as the cause", "could not reach" in detail, True)

(ok, detail), calls = can_push(200, token="")
check("push: no token configured is a no", ok, False)
check("push: asked without spending a request", calls, [])

# ---------- and how that answer reads as a check
check("push check: a yes is not blocking", push_check(True, "fine").ok, True)
check("push check: a no is blocking, not a warning", push_check(False, "nope").fatal, True)
check("push check: a no keeps the detail it was given", push_check(False, "nope").detail, "nope")
check("push check: a repo whose token cannot push is not ready",
      report("r", [push_check(False, "nope")]), False)


print()
print(f"{len(fails)} failed" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
