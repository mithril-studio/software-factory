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

from control import db, goldens, runner
from control.agents import (
    BASE,
    BASE_SNAPSHOT,
    GOLDEN_PREFIX,
    api_rows,
    available,
    forget,
    golden_name,
    is_golden,
    listed,
    parse_golden,
    quarantined,
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

# ---------- quarantine: what the runs prove, and what they emphatically do not
#
# Two runs stalled on a warm golden that no run had ever proved, while the control plane went
# on handing it to every dispatch and logging `no run has yet proved` into a void. These pin
# the rule that closes that loop — and, just as hard, the three ways of knowing nothing, none
# of which may read as a verdict. `resolve_snapshot` demoting on an absent measurement would
# be the same error as reading an unreachable box as a frozen one.

FAILED = {"last_verdict": "failed", "verdict_run": "r-d136841b"}
PROVED = {"last_verdict": "failed", "verdict_run": "r-d136841b", "verified_at": "2026-08-20T09:00:00Z"}
PASSED = {"last_verdict": "succeeded", "verdict_run": "r-6fa230d0"}

check("quarantined: a golden whose only verdict is a failure is skipped",
      bool(quarantined(WARM, {WARM: FAILED})), True)
check("quarantined: a golden a run has proved is kept, whatever its last run did",
      quarantined(WARM, {WARM: PROVED}), "")
check("quarantined: a passing verdict is not a reason to skip anything",
      quarantined(WARM, {WARM: PASSED}), "")
check("quarantined: a cancelled run is no verdict at all, so it demotes nothing",
      quarantined(WARM, {WARM: {"last_run": "r-cancelled", "ok": 0}}), "")
check("quarantined: no entry for this golden is unproven, which is not bad",
      quarantined(WARM, {"golden-acme-api": FAILED}), "")
check("quarantined: no evidence at all is unknown, and unknown is never a demotion",
      quarantined(WARM, None), "")
check("quarantined: the reason names the run, because a skip nobody can trace is a rumour",
      "r-d136841b" in quarantined(WARM, {WARM: FAILED}), True)

check("resolve_snapshot: a warm golden with only a failure behind it falls back to the base",
      resolve_snapshot(REPO, FLEET, {WARM: FAILED}), BASE_SNAPSHOT)
check("resolve_snapshot: one run proving it is enough to keep booting it",
      resolve_snapshot(REPO, FLEET, {WARM: PROVED}), WARM)
check("resolve_snapshot: and a succeeding run un-quarantines it",
      resolve_snapshot(REPO, FLEET, {WARM: PASSED}), WARM)
check("resolve_snapshot: evidence that says nothing about this golden still boots it",
      resolve_snapshot(REPO, FLEET, {}), WARM)
check("resolve_snapshot: no evidence passed at all is today's behaviour, unchanged",
      resolve_snapshot(REPO, FLEET, None), WARM)
# The base image is never quarantined by this path: `resolve_snapshot` only ever asks about
# the warm tier, because demoting the thing there is no fallback from would leave a repo with
# nothing to boot at all.
check("resolve_snapshot: a failed base image is still the fallback, since there is no other",
      resolve_snapshot("brand/new", FLEET, {BASE_SNAPSHOT: FAILED}), BASE_SNAPSHOT)

# ---------- api rows
# What the fleet page is handed. The rows come from the refresh loop's table, which is the
# only registry there is — so this is a pure function over rows, and the whole thing is
# testable without a database, a fleet or a clock.

KNOWN = {
    BASE_SNAPSHOT: {
        "repo": None, "agent": "claude", "version": "3", "events": "claude-code",
        "agent_version": "2.1", "ok": 1, "error": None,
        "verified_at": "2026-08-18T10:00:00+00:00",
        "manifest": "{}", "checked_at": "2026-08-18T12:00:00+00:00",
    },
    "golden-acme-api": {
        "repo": "acme-api", "agent": "codex", "version": "1", "events": "codex",
        "agent_version": None, "ok": 0, "error": "boom", "verified_at": None,
        "manifest": None, "checked_at": "2026-08-18T12:00:00+00:00",
    },
    WARM: {
        "repo": "mithril-studio-software-factory", "agent": None, "version": "7",
        "events": None, "agent_version": None, "ok": 0, "error": None, "verified_at": None,
        "manifest": None, "checked_at": "2026-08-18T12:00:00+00:00",
    },
}

rows = api_rows(KNOWN)
check("api rows: the base sorts first, because an empty list below it is explained by it",
      [r["snapshot"] for r in rows], [BASE_SNAPSHOT, "golden-acme-api", WARM])
check("api rows: nothing is invented and nothing is dropped", len(rows), len(KNOWN))
check("api rows: the base is marked as one, and only the base",
      {r["snapshot"]: r["base"] for r in rows},
      {BASE_SNAPSHOT: True, "golden-acme-api": False, WARM: False})
check("api rows: each row carries what the refresh recorded",
      {k: rows[0][k] for k in ("agent", "version", "events", "agent_version", "ok", "error")},
      {"agent": "claude", "version": "3", "events": "claude-code", "agent_version": "2.1",
       "ok": True, "error": None})
check("api rows: verified_at is passed through as the evidence it is",
      rows[0]["verified_at"], "2026-08-18T10:00:00+00:00")
check("api rows: a golden no run has proved reads unproven, not broken",
      (rows[2]["ok"], rows[2]["error"], rows[2]["verified_at"]), (False, None, None))
check("api rows: a golden whose last run failed carries that error",
      (rows[1]["ok"], rows[1]["error"]), (False, "boom"))
check("api rows: the columns are exactly what the page asks for",
      sorted(rows[0]),
      ["agent", "agent_version", "base", "error", "events", "ok", "ready", "repo", "snapshot",
       "status", "verified_at", "version"])
check("api rows: a golden with a version is bootable whatever it is currently doing",
      [(r["snapshot"], r["ready"]) for r in api_rows({
          BASE_SNAPSHOT: {"version": "1", "status": "ready"},
          "golden-a-b": {"version": "3", "status": "pending"},
          "golden-c-d": {"version": None, "status": "pending"},
      })],
      [(BASE_SNAPSHOT, True), ("golden-a-b", True), ("golden-c-d", False)])
check("api rows: the base row names no repo, because it serves every repo",
      rows[0]["repo"], None)
check("api rows: a row that recorded no agent says so rather than guessing one from the name",
      rows[2]["agent"], None)
check("api rows: the repo falls back to the slug its name carries",
      api_rows({"golden-acme-api": {}})[0]["repo"], "acme-api")

# The registry is the name. A row for something that is not a golden is a row the refresh
# should never have written, and rendering it would put a machine in a table of goldens.
check("api rows: a row whose name is not a golden is dropped",
      [r["snapshot"] for r in api_rows({"goldenrod": {}, BASE_SNAPSHOT: {}})],
      [BASE_SNAPSHOT])
check("api rows: an empty table is an empty list, not an error", api_rows({}), [])


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


# ---------- refresh
# The loop that replaced the freshness sweep. It reads the fleet and writes what it saw; the
# grading is done from rows the runs already wrote, so nothing here boots a VM. Both the
# database and boxd are stubbed — a golden's health is a question about evidence, and
# evidence is exactly what a test can hand it.

recorded: dict[str, dict] = {}
evidence: dict[str, dict] = {}


async def fake_record_snapshot(name, **fields):
    recorded[name] = fields


async def fake_evidence():
    return evidence


def run_refresh(names, seen=None):
    """Run one refresh against a stubbed fleet and a stubbed database."""
    recorded.clear()
    evidence.clear()
    evidence.update(seen or {})
    real = (runner.client, db.record_snapshot, db.snapshot_evidence)
    runner.client = lambda: FakeBoxd(names)
    db.record_snapshot, db.snapshot_evidence = fake_record_snapshot, fake_evidence
    try:
        return asyncio.run(goldens.refresh())
    finally:
        runner.client, db.record_snapshot, db.snapshot_evidence = real
        forget()


FLEET_ROWS = [BASE_SNAPSHOT, WARM, "golden-acme-api", "goldenrod", "software-factory"]

rows = run_refresh(FLEET_ROWS)
check("refresh: one row per discovered golden, and none for anything else",
      sorted(recorded), sorted([BASE_SNAPSHOT, WARM, "golden-acme-api"]))
check("refresh: what it returns is what it wrote", sorted(rows), sorted(recorded))
check("refresh: each row names the repo its snapshot name carries",
      recorded[WARM]["repo"], "mithril-studio-software-factory")
check("refresh: and the base names none", recorded[BASE_SNAPSHOT]["repo"], None)
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
    BASE_SNAPSHOT: {
        "last_run": "r1", "ok": 1, "error": None, "verified_at": "2026-08-18T10:00:00+00:00",
        "manifest": '{"agent": "codex", "events": "codex", "transcript": "/t/*.jsonl", '
                    '"version": "0.4.1"}',
    },
    "golden-acme-api": {"last_run": "r2", "ok": 0, "error": "boom", "manifest": None},
})
check("refresh: a golden a run finished on carries that run's verdict",
      (recorded[BASE_SNAPSHOT]["ok"], recorded[BASE_SNAPSHOT]["last_run"]), (1, "r1"))
check("refresh: verified_at is evidence from a run, not a probe",
      recorded[BASE_SNAPSHOT]["verified_at"], "2026-08-18T10:00:00+00:00")
check("refresh: the manifest the golden announced is unpacked into columns",
      (recorded[BASE_SNAPSHOT]["events"], recorded[BASE_SNAPSHOT]["transcript"],
       recorded[BASE_SNAPSHOT]["agent_version"]),
      ("codex", "/t/*.jsonl", "0.4.1"))
# The whole point of taking the agent out of the name: which agent an image runs is what the
# image said, not what somebody called the snapshot.
check("refresh: the agent comes from the manifest and from nowhere else",
      (recorded[BASE_SNAPSHOT]["agent"], recorded["golden-acme-api"]["agent"]), ("codex", None))
check("refresh: a failed last run is reported as the error it was",
      (recorded["golden-acme-api"]["ok"], recorded["golden-acme-api"]["error"]), (0, "boom"))
check("refresh: an unreadable manifest costs the columns, not the row",
      (recorded["golden-acme-api"]["events"], recorded["golden-acme-api"]["agent_version"]),
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
real_record, real_evidence = db.record_snapshot, db.snapshot_evidence
runner.client = lambda: BrokenBoxd([])
db.record_snapshot, db.snapshot_evidence = fake_record_snapshot, fake_evidence
try:
    check("refresh: an unlistable fleet returns nothing and raises nothing",
          asyncio.run(goldens.refresh()), {})
finally:
    runner.client = real_client
    db.record_snapshot, db.snapshot_evidence = real_record, real_evidence
    forget()
check("refresh: and writes no rows", recorded, {})

print()
print(f"{len(fails)} failed" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
