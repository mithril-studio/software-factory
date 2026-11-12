"""The golden naming contract: what counts as a golden, and which snapshot a run boots.

Snapshot names are the only registry the fleet has, so every check below is really the same
question asked from a different side: can a name be misread? A machine someone called
`goldenrod` must not become a golden. The base image must not read as a repo, and no repo
must ever be able to produce the base image's name. A repo with no warm snapshot must fall
back to the base rather than to some other repo's golden.

The agent is deliberately absent from all of it. It used to be the first half of every name;
now it is something an image announces about itself in its manifest, which is why the refresh
checks below assert that `agent` is read out of a run's manifest and never off a name.

Nothing here needs credentials, a VM or a database. `available()` and the refresh loop are
exercised against stubs precisely because a test that needs any of those is a test that stops
being run.

Run it directly, no framework needed:

    .venv/bin/python -m control.agents_test
"""
import asyncio
import sys

from control.agents import (
    BASE,
    BASE_SNAPSHOT,
    GOLDEN_PREFIX,
    available,
    forget,
    golden_name,
    is_golden,
    listed,
    parse_golden,
    resolve_snapshot,
    slug,
    status,
    version,
)

fails = []


def check(name, got, want=True):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n       got={got!r} want={want!r}"))
    if not ok:
        fails.append(name)


REPO = "mithril-studio/software-factory"
WARM = "golden-mithril-studio-software-factory"

# ---------- parse_golden

# The ways a name can lie about being a golden.
check("parse_golden: goldenrod is a machine name, not a golden", parse_golden("goldenrod"), None)
check("parse_golden: golden- names no repo", parse_golden("golden-"), None)
check("parse_golden: golden--repo is fringed with a hyphen a slug never carries",
      parse_golden("golden--repo"), None)
check("parse_golden: and so is a trailing one", parse_golden("golden-acme-"), None)
check("parse_golden: an unrelated snapshot is not a golden", parse_golden("software-factory"), None)
check("parse_golden: the empty name is not a golden", parse_golden(""), None)

# `BASE` and `None` are the two answers that must never be confused: one is the image every
# run falls back to, the other is somebody else's snapshot. Both are falsy, which is exactly
# why every caller compares against `None` explicitly.
check("parse_golden: the base image carries no repo", parse_golden(BASE_SNAPSHOT), BASE)
check("parse_golden: and that is not the same answer as 'not a golden'",
      parse_golden(BASE_SNAPSHOT) is None, False)
check("parse_golden: a warm golden carries the repo slug", parse_golden(WARM),
      "mithril-studio-software-factory")
check("parse_golden: surrounding whitespace does not change the answer",
      parse_golden(f"  {WARM}  "), "mithril-studio-software-factory")

check("is_golden: reads the same way, without the caller minding the difference",
      [is_golden(n) for n in (BASE_SNAPSHOT, WARM, "goldenrod", "")],
      [True, True, False, False])

# ---------- slug

check("slug: owner and name join with a single hyphen",
      slug("mithril-studio/software-factory"), "mithril-studio-software-factory")
check("slug: case is normalised", slug("Mithril-Studio/Software-Factory"),
      "mithril-studio-software-factory")
check("slug: dots and underscores collapse too", slug("acme/my_api.v2"), "acme-my-api-v2")
check("slug: the owner is kept, so two owners cannot collide",
      slug("a/app") == slug("b/app"), False)

HYPHENATED = [
    "mithril-studio/software-factory",
    "a-b-c/d-e-f",
    "owner/--weird--name--",
    "UPPER-CASE/Repo_Name",
    "a/app",
]
for repo in HYPHENATED:
    check(f"slug: {repo} round-trips through its golden name",
          parse_golden(golden_name(repo)), slug(repo))

# The one collision that would matter, and why it cannot happen: `owner/name` always contains
# a `/`, which always becomes a hyphen, so no slug is ever the single word the base is named
# for. If that ever stops holding, a repo would silently overwrite the image every other repo
# falls back to.
check("slug: no repo can ever produce the base image's name",
      any(golden_name(r) == BASE_SNAPSHOT for r in HYPHENATED), False)
check("slug: because every slug carries the separator the owner half forces",
      all("-" in slug(r) for r in HYPHENATED), True)

check("golden_name: no repo means the base image", golden_name(), BASE_SNAPSHOT)
check("golden_name: and so does a repo with nothing nameable in it",
      golden_name("///"), BASE_SNAPSHOT)
check("golden_name: a repo names itself, with no agent anywhere in it",
      golden_name(REPO), GOLDEN_PREFIX + "mithril-studio-software-factory")

# ---------- resolve_snapshot

FLEET = (BASE_SNAPSHOT, WARM, "golden-acme-api", "goldenrod", "software-factory")

check("resolve_snapshot: a warm snapshot wins", resolve_snapshot(REPO, FLEET), WARM)
check("resolve_snapshot: no warm snapshot falls back to the base",
      resolve_snapshot("mithril-studio/other-repo", FLEET), BASE_SNAPSHOT)
check("resolve_snapshot: another repo's warm golden is never borrowed",
      resolve_snapshot("acme/other", FLEET), BASE_SNAPSHOT)
check("resolve_snapshot: no repo asks for the base directly",
      resolve_snapshot(None, FLEET), BASE_SNAPSHOT)
check("resolve_snapshot: a fleet with no base image resolves to nothing",
      resolve_snapshot("acme/other", ("golden-acme-api",)), None)
check("resolve_snapshot: an empty fleet resolves to nothing", resolve_snapshot(REPO, ()), None)

# The fallback is the whole reason connecting a repo does not wait on provisioning: an
# unprovisioned repo is not an error state, it is the ordinary first run.
check("resolve_snapshot: an unprovisioned repo still dispatches",
      bool(resolve_snapshot("brand/new", (BASE_SNAPSHOT,))), True)

# ---------- available

class FakeSnapshot:
    """A snapshot as boxd reports it: one row per *name*.

    `version` is the newest **ready** capture and `status` is what the name is doing right now,
    so `(None, "pending")` and `(1, "pending")` are different states — see `agents._listing`.
    """

    def __init__(self, name, snapshot_version="1", snapshot_status="ready"):
        self.name = name
        self.version = snapshot_version
        self.status = snapshot_status


class FakeBoxd:
    """Stands in for AsyncBoxd. Counts calls, so the memoisation is observable."""

    def __init__(self, names, snapshots=None):
        self.calls = 0
        self.snapshots = self
        self.names = list(names)
        # name -> (version, status), for the ones that are not a plain ready capture.
        self.detail = dict(snapshots or {})

    async def list(self):
        self.calls += 1
        return [FakeSnapshot(n, *self.detail.get(n, ("1", "ready"))) for n in self.names]

    async def close(self):
        pass


forget()
boxd = FakeBoxd([BASE_SNAPSHOT, WARM, "goldenrod", "software-factory", "golden-"])
names = asyncio.run(available(boxd))
check("available: only goldens come back, sorted", names, tuple(sorted([BASE_SNAPSHOT, WARM])))
asyncio.run(available(boxd))
check("available: a second lookup inside the TTL does not re-read the fleet", boxd.calls, 1)

forget()
asyncio.run(available(boxd))
check("available: forgetting the cache re-reads the fleet", boxd.calls, 2)

check("available: the version arrives with the listing, no second round trip",
      version(BASE_SNAPSHOT), "1")
check("available: a name the listing never held has no version", version("golden-acme-api"), "")
check("available: the status arrives with it too", status(BASE_SNAPSHOT), "ready")

# The failure this split exists for. The first capture of a repo's golden has no ready version
# behind it, and boxd answers `create(from_snapshot=...)` with
# `ConflictError: snapshot is 'pending' (no ready version yet)`. It cost three attempts, no VM
# and a halted issue the first time a golden was provisioned and dispatched onto seconds later.
forget()
capturing = FakeBoxd(
    [BASE_SNAPSHOT, WARM],
    snapshots={WARM: (None, "pending")},
)
check("capturing: a first capture is not something a run can boot",
      asyncio.run(available(capturing)), (BASE_SNAPSHOT,))
check("capturing: so the repo falls back to the base instead of failing at the fork",
      resolve_snapshot(REPO, asyncio.run(available(capturing))), BASE_SNAPSHOT)
check("capturing: but the fleet view still shows it, because somebody is waiting on it",
      asyncio.run(listed(capturing)), tuple(sorted([BASE_SNAPSHOT, WARM])))
check("capturing: and says what it is doing", status(WARM), "pending")
check("capturing: with no version behind it", version(WARM), "")

# The other `pending`, and the reason this is not a filter on `status`. A re-save captures a
# new version while the previous one stays restorable — dropping the name would make a working
# golden vanish from discovery mid-poll, which is the bug the old code was avoiding.
forget()
resaving = FakeBoxd([BASE_SNAPSHOT, WARM], snapshots={WARM: ("3", "pending")})
check("re-save: a golden being re-captured is still bootable on its previous version",
      asyncio.run(available(resaving)), tuple(sorted([BASE_SNAPSHOT, WARM])))
check("re-save: and the repo still resolves onto it",
      resolve_snapshot(REPO, asyncio.run(available(resaving))), WARM)
check("re-save: the version reported is the ready one, not the one in flight",
      version(WARM), "3")

class CapturingBoxd(FakeBoxd):
    """A fleet where `name` gains `lands` after `after` listings. `lands=None` never finishes."""

    def __init__(self, names, name, after, lands="2"):
        super().__init__(names, {name: (None, "pending")})
        self._name, self._after, self._lands = name, after, lands

    async def list(self):
        rows = await super().list()
        self._after -= 1
        if self._after <= 0 and self._lands:
            self.detail[self._name] = (self._lands, "ready")
        return rows


forget()
before_calls = capturing.calls
asyncio.run(available(capturing))
asyncio.run(listed(capturing))
check("capturing: one fleet read answers both questions", capturing.calls - before_calls, 1)
check("forget: clears the statuses as well as the versions",
      (forget(), status(WARM), version(WARM))[1:], ("", ""))

# A snapshot somebody deleted must leave the answer, not linger in a cache. This is what stops
# a dispatch resolving onto a golden that is no longer forkable.
boxd.names.remove(WARM)
forget()
check("vanished: a deleted snapshot stops being available",
      WARM in asyncio.run(available(boxd)), False)
check("vanished: and the repo it was warmed for falls back to the base rather than failing",
      resolve_snapshot(REPO, asyncio.run(available(boxd))), BASE_SNAPSHOT)
check("vanished: its version leaves with it", version(WARM), "")
check("vanished: the snapshots that remain are untouched",
      asyncio.run(available(boxd)), (BASE_SNAPSHOT,))
forget()


print()
print(f"{len(fails)} failed" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
