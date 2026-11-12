"""The register of watched repos, and the branch their pull requests target.

The list the poller reads can change while it runs, so the seed must be additive, the cache
and the table must never disagree, and the target branch must resolve the same way for the
poller, the run and the UI.

Touches SQLite, against a throwaway file. Run it directly, no framework needed:

    .venv/bin/python -m control.repos_test
"""
import asyncio
import sys
import tempfile
from pathlib import Path

from control import db, github, repos
from control.config import settings

fails = []


def check(name, got, want=True):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n       got={got!r} want={want!r}"))
    if not ok:
        fails.append(name)


tmp = tempfile.TemporaryDirectory()
object.__setattr__(settings, "db_path", Path(tmp.name) / "factory.db")
asyncio.run(db.init())


def run(coro):
    return asyncio.run(coro)


# ---------- validation
check("valid: owner/name is what GitHub means", repos.valid("mithril-studio/software-factory"))
check("valid: dots, dashes and underscores in the name are fine", repos.valid("acme/my_api.v2"))
check("valid: a bare name names no owner", repos.valid("software-factory"), False)
check("valid: a URL is not a repo", repos.valid("https://github.com/a/b"), False)
check("valid: a third segment is not a repo", repos.valid("a/b/c"), False)
check("valid: the empty string is not a repo", repos.valid(""), False)


# ---------- seed
def seed(*entries):
    object.__setattr__(settings, "repos", tuple(entries))
    run(repos.seed())
    return repos.watched()


check("seed: FACTORY_REPOS fills an empty register",
      seed("acme/api", "acme/web"), ("acme/api", "acme/web"))
check("seed: a repo nobody could parse is skipped rather than watched",
      seed("acme/api", "not a repo", "acme/web"), ("acme/api", "acme/web"))
run(repos.add("connected/through-the-api"))
check("seed: a repo connected through the UI survives a restart",
      "connected/through-the-api" in seed("acme/api", "acme/web"))
check("seed: dropping an entry from FACTORY_REPOS does not unwatch it",
      "acme/web" in seed("acme/api"))

# ---------- add, remove, and the cache over them
before = repos.watched()
run(repos.add("new/repo"))
check("add: watched immediately", repos.watched()[-1], "new/repo")
check("add: and nothing else moved", repos.watched()[:len(before)], before)
check("add: adding it twice leaves one entry",
      (run(repos.add("new/repo")), repos.watched().count("new/repo"))[1], 1)
check("remove: says whether there was anything to remove", run(repos.remove("new/repo")), True)
check("remove: removing something unwatched is False, not an error",
      run(repos.remove("nobody/knows")), False)
check("cache: what the cache holds is what the table holds",
      repos.watched(), tuple(r["repo"] for r in run(db.list_repos())))
check("order: oldest first", repos.watched()[0], "acme/api")

# ---------- target branch
check("target: defaults to FACTORY_TARGET_BRANCH", repos.target_branch("acme/api"), "test")
check("target: an unwatched repo gets the default too", repos.target_branch("x/y"), "test")
run(repos.set_target_branch("acme/api", "develop"))
check("target: a repo can point elsewhere", repos.target_branch("acme/api"), "develop")
run(repos.set_target_branch("acme/api", ""))
check("target: an empty value reverts to the default", repos.target_branch("acme/api"), "test")
for bad in ("../x", "a//b", "trailing/"):
    try:
        run(repos.set_target_branch("acme/api", bad))
        check(f"target: {bad!r} is refused", False)
    except ValueError:
        check(f"target: {bad!r} is refused", True)
try:
    run(repos.set_target_branch("not/watched", "test"))
    check("target: an unwatched repo cannot be configured", False)
except ValueError:
    check("target: an unwatched repo cannot be configured", True)


# ensure_target: exists -> untouched; missing -> created from the default branch's head.
class FakeGitHub:
    def __init__(self, branches, default="main"):
        self.branches, self.default, self.created = dict(branches), default, []

    async def branch_sha(self, repo, branch):
        return self.branches.get(branch)

    async def default_branch(self, repo):
        return self.default

    async def create_branch(self, repo, branch, sha):
        self.created.append((branch, sha))
        self.branches[branch] = sha


def ensure(fake):
    real = (github.branch_sha, github.default_branch, github.create_branch)
    github.branch_sha, github.default_branch, github.create_branch = (
        fake.branch_sha, fake.default_branch, fake.create_branch)
    try:
        return run(repos.ensure_target("acme/api"))
    finally:
        github.branch_sha, github.default_branch, github.create_branch = real


fake = FakeGitHub({"main": "aaa", "test": "bbb"})
check("ensure: an existing target is returned as is", (ensure(fake), fake.created), ("test", []))
fake = FakeGitHub({"main": "aaa"})
check("ensure: a missing target is created", ensure(fake), "test")
check("ensure: from the default branch's head", fake.created, [("test", "aaa")])
fake = FakeGitHub({}, default="main")
try:
    ensure(fake)
    check("ensure: an empty repo is an error, not a guess", False)
except RuntimeError:
    check("ensure: an empty repo is an error, not a guess", True)

print()
print(f"{len(fails)} failed" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
