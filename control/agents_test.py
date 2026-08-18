"""The golden naming contract: what counts as an agent, and which snapshot a run forks.

Snapshot names are the only registry this platform has, so every check below is really the
same question asked from a different side: can a name be misread? A machine someone called
`goldenrod` must not become an agent. A repo full of hyphens must not swallow the separator
and take the agent's name with it. A repo with no warm snapshot must fall back to the agent
image rather than to some other repo's golden.

Nothing here touches boxd. `available()` is exercised against a stub precisely because a
test that needs credentials is a test that stops being run.

Run it directly, no framework needed:

    .venv/bin/python -m control.agents_test
"""
import asyncio
import sys

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
    def __init__(self, name):
        self.name = name


class FakeBoxd:
    """Stands in for AsyncBoxd. Counts calls, so the memoisation is observable."""

    def __init__(self, names):
        self.calls = 0
        self.snapshots = self
        self._names = names

    async def list(self):
        self.calls += 1
        return [FakeSnapshot(n) for n in self._names]


forget()
boxd = FakeBoxd(["golden-claude", WARM, "goldenrod", "software-factory", "golden-"])
names = asyncio.run(available(boxd))
check("available: only goldens come back, sorted", names, tuple(sorted(["golden-claude", WARM])))
asyncio.run(available(boxd))
check("available: a second lookup inside the TTL does not re-read the fleet", boxd.calls, 1)

forget()
asyncio.run(available(boxd))
check("available: forgetting the cache re-reads the fleet", boxd.calls, 2)
forget()

print()
print(f"{len(fails)} failed" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
