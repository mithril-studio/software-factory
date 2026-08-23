"""A pull request GitHub calls conflicted, that git merges cleanly, gets merged.

GitHub's merge API is not git. It does a three-way content merge with no working tree, so it
never reads the repository's `.gitattributes` — and a repo that declares `merge=union` for its
append-only logs gets that driver on every laptop and every CI runner and does not get it
there. The pull request then merges with `git merge` and is refused by GitHub as conflicted,
which is a state nobody can act on by reading it.

foundation-e-learning#82 is the case: review passed on four criteria, every check green, and
the merge refused on one appended line of `.mem/index.jsonl` — the exact file whose
`.gitattributes` entry exists to make that line mergeable. Four issues sat behind it.

The first half of this file is the repair, tested against real git repositories on disk rather
than against a mock, because whether a merge driver applies is precisely the thing a mock
would have to assume. The second half is the routing around it: which failures are repairable,
what happens when the repair does not work, and — the one that matters — that a reconciled
branch is re-verified before it is merged, since reconciling moves the head and a sha nobody
checked is exactly what pinning exists to prevent.

Run it directly, no framework needed:

    .venv/bin/python -m control.merge_reconcile_test
"""
import asyncio
import pathlib
import subprocess
import sys
import tempfile

from control import github, runner
from control.config import settings

fails: list[str] = []


def check(name, got, want=True):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n       got={got!r} want={want!r}"))
    if not ok:
        fails.append(name)


def git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


ATTRIBUTES = "# see foundation-e-learning\n.mem/**/*.jsonl merge=union\n"


def build_origin(tmp: pathlib.Path, *, rival_edit: str | None = None) -> str:
    """A bare origin holding `main` and `topic`, each with one line the other does not have.

    Both branches append to the same append-only log at the same offset, which is what two
    factory runs finishing minutes apart actually produce. `rival_edit` instead puts the
    divergence in an ordinary file, where no driver applies and the conflict is real.
    """
    work = tmp / "work"
    work.mkdir()
    git("init", "-q", "-b", "main", ".", cwd=work)
    git("config", "user.email", "t@example.com", cwd=work)
    git("config", "user.name", "t", cwd=work)
    (work / ".gitattributes").write_text(ATTRIBUTES)
    (work / ".mem").mkdir()
    (work / ".mem" / "index.jsonl").write_text('{"id":"mem_base"}\n')
    (work / "notes.txt").write_text("shared\n")
    git("add", "-A", cwd=work)
    git("commit", "-qm", "base", cwd=work)

    git("checkout", "-qb", "topic", cwd=work)
    if rival_edit is None:
        (work / ".mem" / "index.jsonl").write_text('{"id":"mem_base"}\n{"id":"mem_topic"}\n')
    else:
        (work / "notes.txt").write_text("topic\n")
    git("commit", "-qam", "topic work", cwd=work)

    git("checkout", "-q", "main", cwd=work)
    if rival_edit is None:
        (work / ".mem" / "index.jsonl").write_text('{"id":"mem_base"}\n{"id":"mem_main"}\n')
    else:
        (work / "notes.txt").write_text(rival_edit)
    git("commit", "-qam", "main work", cwd=work)

    bare = tmp / "origin.git"
    git("clone", "-q", "--bare", str(work), str(bare), cwd=tmp)
    # A bare repo refuses a push to the branch it has checked out; it has none, but its HEAD
    # points at main, and pushing to main is not something this ever does anyway.
    return str(bare)


def reconcile(bare: str, branch="topic", base="main"):
    github.clone_url = lambda repo: bare  # noqa: ARG005 - the seam this test exists to use
    lines: list[str] = []
    return asyncio.run(github.merge_base_into_branch("o/r", branch, base, lines.append)), lines


object.__setattr__(settings, "github_token", "test-token")

# ---------------------------------------------------------------- the repair, against real git

with tempfile.TemporaryDirectory() as tmp:
    bare = build_origin(pathlib.Path(tmp))
    main_before = git("rev-parse", "main", cwd=bare)
    (ok, detail), lines = reconcile(bare)
    check("a union-mergeable divergence reconciles", ok)
    check("and says what it did", "merged main into topic" in detail)
    check("and reports it to the run log", any("merge drivers" in ln for ln in lines))

    # The point of the whole exercise: the pushed branch carries *both* runs' records. A
    # driver that dropped one would still merge, and would quietly lose a memory.
    seen = pathlib.Path(tmp) / "seen"
    git("clone", "-q", "-b", "topic", bare, str(seen), cwd=tmp)
    merged = (seen / ".mem" / "index.jsonl").read_text()
    check("the union driver kept both appended lines",
          ("mem_topic" in merged, "mem_main" in merged, "<<<<" in merged),
          (True, True, False))
    # `base` is read and never written. The worst case this can produce is a factory branch
    # nobody wanted, never a moved trunk.
    check("main was not written to", git("rev-parse", "main", cwd=bare), main_before)

with tempfile.TemporaryDirectory() as tmp:
    bare = build_origin(pathlib.Path(tmp), rival_edit="main\n")
    before = git("rev-parse", "topic", cwd=bare)
    (ok, detail), _ = reconcile(bare)
    # A real disagreement is a human's call. Picking a side with `-X ours` would resolve every
    # one of these silently, which is the reason no strategy flag is passed.
    check("a genuine conflict is refused, not resolved", ok, False)
    check("and names the file git could not merge", "notes.txt" in detail)
    check("and pushes nothing", git("rev-parse", "topic", cwd=bare), before)

with tempfile.TemporaryDirectory() as tmp:
    bare = build_origin(pathlib.Path(tmp))
    # A branch that has not diverged was refused for some other reason — protection, review,
    # permission — and none of those is repaired by merging.
    (ok, detail), _ = reconcile(bare, branch="main", base="main")
    check("a branch already up to date is not a reconcile", ok, False)
    check("and says so", "already up to date" in detail)

object.__setattr__(settings, "github_token", "")
check("with no token it refuses before cloning anything",
      asyncio.run(github.merge_base_into_branch("o/r", "topic", "main", lambda _: None)),
      (False, "no GitHub token is configured, so nothing can push"))
object.__setattr__(settings, "github_token", "test-token")


# ---------------------------------------------------------------- which failures are repairable

check("GitHub's conflict wording is recognised",
      github.is_merge_conflict(Exception("merge refused: 405 Pull Request has merge conflicts")))
check("so is the other one it uses",
      github.is_merge_conflict(Exception("merge refused: 405 Pull Request is not mergeable")))
# Reconciling a branch nobody may push to, or one behind a required review, changes nothing and
# costs a clone. These arrive as the same 405.
check("a refused permission is not",
      github.is_merge_conflict(Exception("merge refused: 403 Resource not accessible")), False)
check("nor is a required review",
      github.is_merge_conflict(Exception("merge refused: 405 At least 1 approving review is required")),
      False)


# ---------------------------------------------------------------- routing, with github stubbed

class Log:
    def __init__(self): self.lines = []
    def write(self, line): self.lines.append(line)


def drive(*, merge_errors, reconcile_result=(True, "merged"), shas=("sha1", "sha2")):
    """Run `_merge` with GitHub replaced. Returns (attempt, calls, log)."""
    calls = {"merge": 0, "reconcile": 0, "checks": 0, "ci": 0}
    errors = list(merge_errors)

    async def merge_pr(repo, number, sha=None, **kw):
        calls["merge"] += 1
        exc = errors.pop(0) if errors else None
        if exc:
            raise exc
        calls["merged_sha"] = sha

    async def pr_head_sha(repo, number):
        return shas[min(calls["checks"], len(shas) - 1)]

    async def checks_green(repo, sha, timeout=0):
        calls["checks"] += 1
        calls.setdefault("checked", []).append(sha)
        return True, "2 checks passed", []

    async def merge_base_into_branch(repo, branch, base, write):
        calls["reconcile"] += 1
        return reconcile_result

    async def record_ci(*a, **kw):
        calls["ci"] += 1
        return None

    saved = {k: getattr(github, k) for k in
             ("merge_pr", "pr_head_sha", "checks_green", "merge_base_into_branch")}
    saved_ci, saved_now = runner._record_ci, runner.db.utcnow
    github.merge_pr, github.pr_head_sha = merge_pr, pr_head_sha
    github.checks_green, github.merge_base_into_branch = checks_green, merge_base_into_branch
    runner._record_ci = record_ci
    runner.db.utcnow = lambda: "2026-08-23T00:00:00+00:00"
    log = Log()
    try:
        attempt = asyncio.run(runner._merge(
            "o/r", "https://github.com/o/r/pull/7", "main", log,
            issue={"number": 7, "title": "t"}, branch="factory/issue-7", cycle=1,
        ))
    finally:
        for k, v in saved.items():
            setattr(github, k, v)
        runner._record_ci, runner.db.utcnow = saved_ci, saved_now
    return attempt, calls, log


object.__setattr__(settings, "merge_require_checks", True)

conflict = Exception("merge refused: 405 Pull Request has merge conflicts")

attempt, calls, log = drive(merge_errors=[conflict])
check("a conflicted merge is reconciled and retried", (calls["merge"], calls["reconcile"]), (2, 1))
check("and lands", attempt.merged)
# The regression this is really here for. Reconciling pushes a merge commit, so the head is no
# longer the commit CI passed on; merging the old pinned sha would land code nothing checked,
# and merging without a pin would land whatever arrived meanwhile.
check("the second attempt is checked before it is merged", calls["checks"], 2)
check("and is pinned to the sha that second check saw", calls.get("merged_sha"), "sha2")
check("so the CI phase is recorded for both", calls["ci"], 2)

attempt, calls, _ = drive(merge_errors=[conflict], reconcile_result=(False, "conflicts in notes.txt"))
check("a reconcile that fails leaves the PR for a human", attempt.merged, False)
check("and keeps GitHub's reason, not the reconciler's", "merge conflicts" in attempt.why)
check("and does not merge", calls["merge"], 1)

denied = Exception("merge refused: 403 Resource not accessible by integration")
attempt, calls, _ = drive(merge_errors=[denied])
check("an unrepairable refusal is not reconciled", calls["reconcile"], 0)
check("and stops for a human", attempt.merged, False)

attempt, calls, _ = drive(merge_errors=[conflict, conflict])
# Something is moving underneath this. A third attempt races it.
check("a merge that conflicts again after reconciling stops",
      (calls["merge"], calls["reconcile"], attempt.merged), (2, 1, False))

attempt, calls, _ = drive(merge_errors=[])
check("an ordinary merge still reconciles nothing", calls["reconcile"], 0)
check("and merges once", (calls["merge"], attempt.merged), (1, True))


print()
if fails:
    print(f"{len(fails)} failed: " + ", ".join(fails))
    sys.exit(1)
print("all passed")
