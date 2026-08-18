"""The golden naming contract, and the agent discovery that falls out of it.

A golden is a boxd snapshot, and its *name* carries the whole contract:

    golden-<agent>                 the agent image — tooling and auth, no repo
    golden-<agent>--<repo-slug>    that image with one repo already cloned and installed

There is no registry anywhere else. An agent exists because a snapshot naming it exists, so
listing the fleet and discovering the agents are the same act. That is why the separator is
two hyphens: a repo slug collapses every run of non-alphanumerics down to one, so a slug can
never contain `--`, and the split back into (agent, repo) is unambiguous no matter how many
hyphens the owner or the repo name carries.

Everything here is a pure function over strings apart from `available()`, which is the one
place that talks to boxd. That is deliberate: dispatch decisions can then be tested without
credentials, a VM, a database or a clock.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable

GOLDEN_PREFIX = "golden-"
SEP = "--"
DEFAULT_AGENT = "claude"

# How long a fleet listing is reused. Short on purpose: step 3 creates a snapshot and then
# wants to dispatch onto it moments later, so a stale cache would hide the agent that was
# just built. Ten seconds still collapses the burst of lookups inside a single poll.
CACHE_TTL = 10.0

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slug(repo: str) -> str:
    """`owner/name` to the repo half of a golden name.

    Lowercase, every run of non-alphanumerics collapsed to a single hyphen. The owner stays
    in: two owners with a repo of the same name must not land on the same snapshot.
    """
    return _SLUG_RE.sub("-", (repo or "").lower()).strip("-")


def golden_name(agent: str, repo: str | None = None) -> str:
    """The snapshot name for an agent, warmed for one repo when given. Inverse of `parse_golden`."""
    name = f"{GOLDEN_PREFIX}{agent}"
    repo_slug = slug(repo) if repo else ""
    return f"{name}{SEP}{repo_slug}" if repo_slug else name


def _well_formed(part: str) -> bool:
    """A name part is usable only if it is non-empty and not fringed with hyphens."""
    return bool(part) and not part.startswith("-") and not part.endswith("-")


def parse_golden(name: str) -> tuple[str, str | None] | None:
    """`(agent, repo_slug_or_None)` for a golden name, or `None` when the name is not one.

    Strict by design — this is what stops an unrelated snapshot from being read as an agent.
    `goldenrod` is a machine someone named; `golden-` names no agent; `golden-claude--`
    promises a repo and then does not name it.
    """
    name = (name or "").strip()
    if not name.startswith(GOLDEN_PREFIX):
        return None
    agent, sep, repo_slug = name[len(GOLDEN_PREFIX):].partition(SEP)
    if not _well_formed(agent):
        return None
    if sep and not _well_formed(repo_slug):
        return None
    return agent, repo_slug or None


def discover(available: Iterable[str] = ()) -> tuple[str, ...]:
    """Every distinct agent named by a collection of snapshot names, sorted."""
    return tuple(sorted({found[0] for n in available or () if (found := parse_golden(n))}))


def resolve_snapshot(agent: str, repo: str | None, available: Iterable[str] = ()) -> str | None:
    """Which snapshot a run for `repo` should fork: warm first, then the bare agent image.

    `None` means this agent has no image at all, which is a configuration error the caller
    must surface rather than paper over with somebody else's golden.
    """
    if not agent:
        return None
    names = set(available or ())
    warm = golden_name(agent, repo) if repo else None
    if warm and warm in names:
        return warm
    image = golden_name(agent)
    return image if image in names else None


def resolve_agent(
    repo: str,
    watched: dict | None = None,
    override: str | None = None,
    default: str | None = None,
    available: Iterable[str] = (),
) -> str | None:
    """Which agent should take an issue in `repo`.

    Most specific wins: an explicit `override` for this run, then whatever the repo was
    configured with in `watched`, then the deployment's `default`. Those three are taken at
    their word and never checked against the fleet — a snapshot may be mid-build, and
    silently substituting a different agent is worse than failing on a name that is missing.

    Only past that does the fleet speak. `claude` is the default when it exists, and when
    nothing at all was discovered (an empty listing means "we do not know", not "no agents").
    A deployment that runs one other agent and never says so gets that one. Two candidates
    and no way to choose is `None`: the caller asks a human rather than guessing.
    """
    for choice in (override, (watched or {}).get(repo), default):
        if choice and choice.strip():
            return choice.strip()
    found = discover(available)
    if not found or DEFAULT_AGENT in found:
        return DEFAULT_AGENT
    return found[0] if len(found) == 1 else None


_cache: tuple[float, tuple[str, ...]] | None = None


async def available(boxd) -> tuple[str, ...]:
    """Golden snapshot names in the fleet, memoised for `CACHE_TTL` seconds.

    `boxd` is an `AsyncBoxd`, left untyped so this module imports no third-party package and
    the rest of it can be exercised without one.

    Status is deliberately not filtered on. Re-saving a golden bumps its version while the
    previous one stays forkable, so dropping a name because a newer capture is in flight
    would make a working agent vanish from discovery mid-poll.
    """
    global _cache
    now = time.monotonic()
    if _cache and now - _cache[0] < CACHE_TTL:
        return _cache[1]
    names = tuple(sorted(s.name for s in await boxd.snapshots.list() if parse_golden(s.name)))
    _cache = (now, names)
    return names


def forget() -> None:
    """Drop the memoised listing, so the next `available()` re-reads the fleet."""
    global _cache
    _cache = None
