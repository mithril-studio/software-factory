"""The golden naming contract: what counts as an agent, and which snapshot a run forks.

Snapshot names are the only registry this platform has, so every check below is really the
same question asked from a different side: can a name be misread? A machine someone called
`goldenrod` must not become an agent. A repo full of hyphens must not swallow the separator
and take the agent's name with it. A repo with no warm snapshot must fall back to the agent
image rather than to some other repo's golden.

Nothing here needs credentials, a VM or a database. `available()` and the refresh loop are
exercised against stubs precisely because a test that needs any of those is a test that stops
being run.

Run it directly, no framework needed:

    .venv/bin/python -m control.agents_test
"""
import asyncio
import sys

from control import db, goldens, runner
from control.agents import (
    DEFAULT_AGENT,
    GOLDEN_PREFIX,
    SEP,
    available,
    discover,
    forget,
    golden_name,
    parse_golden,
    resolve_agent,
    resolve_snapshot,
    slug,
    version,
)

fails = []


def check(name, got, want=True):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n       got={got!r} want={want!r}"))
    if not ok:
        fails.append(name)


# ---------- parse_golden

# The two ways a name lies about being an agent. Both are AC1.
check("parse_golden: goldenrod is a machine name, not an agent", parse_golden("goldenrod"), None)
check("parse_golden: golden- names no agent", parse_golden("golden-"), None)
check("parse_golden: golden-claude-- promises a repo it never names",
      parse_golden("golden-claude--"), None)
check("parse_golden: golden--repo has no agent either", parse_golden("golden--repo"), None)
check("parse_golden: an unrelated snapshot is not an agent", parse_golden("software-factory"), None)
check("parse_golden: the empty name is not an agent", parse_golden(""), None)

check("parse_golden: the bare agent image", parse_golden("golden-claude"), ("claude", None))
check("parse_golden: a warm golden carries both halves",
      parse_golden("golden-claude--mithril-studio-software-factory"),
      ("claude", "mithril-studio-software-factory"))
check("parse_golden: another agent parses the same way",
      parse_golden("golden-codex--acme-api"), ("codex", "acme-api"))
check("parse_golden: surrounding whitespace does not change the answer",
      parse_golden("  golden-claude  "), ("claude", None))

# ---------- slug

check("slug: owner and name join with a single hyphen",
      slug("mithril-studio/software-factory"), "mithril-studio-software-factory")
check("slug: case is normalised", slug("Mithril-Studio/Software-Factory"),
      "mithril-studio-software-factory")
check("slug: dots and underscores collapse too", slug("acme/my_api.v2"), "acme-my-api-v2")
check("slug: the owner is kept, so two owners cannot collide",
      slug("a/app") == slug("b/app"), False)

# AC4: the separator survives a repo that is nothing but hyphens.
HYPHENATED = [
    "mithril-studio/software-factory",
    "a-b-c/d-e-f",
    "owner/--weird--name--",
    "UPPER-CASE/Repo_Name",
]
for repo in HYPHENATED:
    name = golden_name("claude", repo)
    check(f"slug: {repo} round-trips without losing the agent",
          parse_golden(name), ("claude", slug(repo)))
    check(f"slug: {repo} never contains the separator", SEP in slug(repo), False)

check("slug: a golden name for no repo is just the agent image",
      golden_name("claude"), GOLDEN_PREFIX + "claude")

# ---------- resolve_snapshot

REPO = "mithril-studio/software-factory"
WARM = "golden-claude--mithril-studio-software-factory"
IMAGE = "golden-claude"
FLEET = (IMAGE, WARM, "golden-codex", "goldenrod", "software-factory")

# AC2, both directions.
check("resolve_snapshot: a warm snapshot wins", resolve_snapshot("claude", REPO, FLEET), WARM)
check("resolve_snapshot: no warm snapshot falls back to the agent image",
      resolve_snapshot("claude", "mithril-studio/other-repo", FLEET), IMAGE)
check("resolve_snapshot: another agent's warm golden is never borrowed",
      resolve_snapshot("codex", REPO, FLEET), "golden-codex")
check("resolve_snapshot: no repo asks for the image directly",
      resolve_snapshot("claude", None, FLEET), IMAGE)
check("resolve_snapshot: an agent with no image at all is a configuration error",
      resolve_snapshot("cursor", REPO, FLEET), None)
check("resolve_snapshot: an empty fleet resolves to nothing",
      resolve_snapshot("claude", REPO, ()), None)
check("resolve_snapshot: no agent resolves to nothing", resolve_snapshot("", REPO, FLEET), None)

# ---------- discover

check("discover: each agent is named once, sorted", discover(FLEET), ("claude", "codex"))
check("discover: nothing in the fleet, nothing discovered", discover(()), ())

# ---------- resolve_agent

WATCHED = {"mithril-studio/other-repo": "codex"}

# AC3: nothing anywhere names an agent, so the default takes it.
check("resolve_agent: a repo that names no agent anywhere resolves to claude",
      resolve_agent(REPO, {}), DEFAULT_AGENT)
check("resolve_agent: claude is still the answer when it is the discovered agent",
      resolve_agent(REPO, {}, available=FLEET), "claude")

check("resolve_agent: an override wins over everything",
      resolve_agent(REPO, WATCHED, override="cursor", default="codex", available=FLEET), "cursor")
check("resolve_agent: the repo's own entry beats the deployment default",
      resolve_agent("mithril-studio/other-repo", WATCHED, default="claude"), "codex")
check("resolve_agent: the deployment default beats the built-in one",
      resolve_agent(REPO, WATCHED, default="codex"), "codex")
check("resolve_agent: blank choices are not choices",
      resolve_agent(REPO, {REPO: "  "}, override="", default=None), DEFAULT_AGENT)
check("resolve_agent: surrounding whitespace is trimmed off a choice",
      resolve_agent(REPO, {}, override=" codex "), "codex")

# Past the explicit choices the fleet decides, and only when it can decide alone.
check("resolve_agent: one discovered agent and no claude is unambiguous",
      resolve_agent(REPO, {}, available=("golden-codex", "golden-codex--acme-api")), "codex")
check("resolve_agent: two discovered agents and no claude asks a human",
      resolve_agent(REPO, {}, available=("golden-codex", "golden-cursor")), None)

# ---------- available

class FakeSnapshot:
    def __init__(self, name, snapshot_version="1"):
        self.name = name
        self.version = snapshot_version


class FakeBoxd:
    """Stands in for AsyncBoxd. Counts calls, so the memoisation is observable."""

    def __init__(self, names):
        self.calls = 0
        self.snapshots = self
        self.names = list(names)

    async def list(self):
        self.calls += 1
        return [FakeSnapshot(n) for n in self.names]

    async def close(self):
        pass


forget()
boxd = FakeBoxd(["golden-claude", WARM, "goldenrod", "software-factory", "golden-"])
names = asyncio.run(available(boxd))
check("available: only goldens come back, sorted", names, tuple(sorted(["golden-claude", WARM])))
asyncio.run(available(boxd))
check("available: a second lookup inside the TTL does not re-read the fleet", boxd.calls, 1)

forget()
asyncio.run(available(boxd))
check("available: forgetting the cache re-reads the fleet", boxd.calls, 2)

check("available: the version arrives with the listing, no second round trip",
      version("golden-claude"), "1")
check("available: a name the listing never held has no version", version("golden-codex"), "")

# AC2: a snapshot somebody deleted must leave the answer, not linger in a cache. This is what
# stops a dispatch resolving onto a golden that is no longer forkable.
boxd.names.remove(WARM)
forget()
check("vanished: a deleted snapshot stops being available",
      WARM in asyncio.run(available(boxd)), False)
check("vanished: and stops being resolvable for the repo it was warmed for",
      resolve_snapshot("claude", REPO, asyncio.run(available(boxd))), IMAGE)
check("vanished: its version leaves with it", version(WARM), "")
check("vanished: the snapshots that remain are untouched",
      asyncio.run(available(boxd)), ("golden-claude",))
forget()


# ---------- refresh  (AC1)
# The loop that replaced the freshness sweep. It reads the fleet and writes what it saw; the
# grading is done from rows the runs already wrote, so nothing here boots a VM. Both the
# database and boxd are stubbed — a golden's health is a question about evidence, and
# evidence is exactly what a test can hand it.

recorded: dict[str, dict] = {}
evidence: dict[str, dict] = {}


async def fake_record_agent(name, **fields):
    recorded[name] = fields


async def fake_evidence():
    return evidence


def run_refresh(names, seen=None):
    """Run one refresh against a stubbed fleet and a stubbed database."""
    recorded.clear()
    evidence.clear()
    evidence.update(seen or {})
    real = (runner.client, db.record_agent, db.agent_evidence)
    runner.client = lambda: FakeBoxd(names)
    db.record_agent, db.agent_evidence = fake_record_agent, fake_evidence
    try:
        return asyncio.run(goldens.refresh())
    finally:
        runner.client, db.record_agent, db.agent_evidence = real
        forget()


FLEET_ROWS = ["golden-claude", WARM, "golden-codex", "goldenrod", "software-factory"]

rows = run_refresh(FLEET_ROWS)
check("refresh: one row per discovered golden, and none for anything else",
      sorted(recorded), sorted(["golden-claude", WARM, "golden-codex"]))
check("refresh: what it returns is what it wrote", sorted(rows), sorted(recorded))
check("refresh: each row names the agent its snapshot name carries",
      recorded[WARM]["agent"], "claude")
check("refresh: and carries the version the listing saw", recorded[WARM]["version"], "1")
check("refresh: a golden nothing has run on is unproven, not broken",
      (recorded[WARM]["ok"], recorded[WARM]["error"], recorded[WARM]["verified_at"]),
      (0, None, None))
check("refresh: every row is stamped with when the fleet was listed",
      all(r["checked_at"] for r in recorded.values()), True)

# Upsert, not append: listing the same fleet twice is one row per name, still.
before = sorted(recorded)
rows = run_refresh(FLEET_ROWS)
check("refresh: listing the same fleet again writes the same one row per name",
      sorted(recorded), before)

# The grade comes from the runs, including the manifest the golden announced on the way in.
rows = run_refresh(FLEET_ROWS, {
    "golden-claude": {
        "last_run": "r1", "ok": 1, "error": None, "verified_at": "2026-08-18T10:00:00+00:00",
        "manifest": '{"agent": "codex", "events": "codex", "transcript": "/t/*.jsonl", '
                    '"version": "0.4.1"}',
    },
    "golden-codex": {"last_run": "r2", "ok": 0, "error": "boom", "manifest": None},
})
check("refresh: a golden a run finished on carries that run's verdict",
      (recorded["golden-claude"]["ok"], recorded["golden-claude"]["last_run"]), (1, "r1"))
check("refresh: verified_at is evidence from a run, not a probe",
      recorded["golden-claude"]["verified_at"], "2026-08-18T10:00:00+00:00")
check("refresh: the manifest the golden announced is unpacked into columns",
      (recorded["golden-claude"]["events"], recorded["golden-claude"]["transcript"],
       recorded["golden-claude"]["agent_version"]),
      ("codex", "/t/*.jsonl", "0.4.1"))
check("refresh: a failed last run is reported as the error it was",
      (recorded["golden-codex"]["ok"], recorded["golden-codex"]["error"]), (0, "boom"))
check("refresh: an unreadable manifest costs the columns, not the row",
      (recorded["golden-codex"]["events"], recorded["golden-codex"]["agent_version"]),
      (None, None))
check("refresh: evidence for a snapshot the fleet no longer holds is not written",
      "golden-gone" in recorded, False)

# A fleet nobody can list is a refresh that recorded nothing, never a control plane that fell
# over: this loop runs unattended every five minutes.
class BrokenBoxd(FakeBoxd):
    async def list(self):
        raise RuntimeError("boxd unreachable")


recorded.clear()
real_client = runner.client
real_record, real_evidence = db.record_agent, db.agent_evidence
runner.client = lambda: BrokenBoxd([])
db.record_agent, db.agent_evidence = fake_record_agent, fake_evidence
try:
    check("refresh: an unlistable fleet returns nothing and raises nothing",
          asyncio.run(goldens.refresh()), {})
finally:
    runner.client = real_client
    db.record_agent, db.agent_evidence = real_record, real_evidence
    forget()
check("refresh: and writes no rows", recorded, {})

print()
print(f"{len(fails)} failed" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
