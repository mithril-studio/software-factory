"""A repo's integration branch is one answer, asked for in one place.

Every dispatch path used to call `github.default_branch` for itself — six call sites across the
runner, the planner and provisioning — which made a repo's default branch its integration branch
by construction. A repo that keeps `main` for production and integrates on `develop` could not
be pointed anywhere else, and a seventh call site added later would have quietly gone back to
`main` while the other six went to `develop`: a planner reading one branch's goal while its
builds land on another.

So `repos.base_branch` is the answer, and this file walks the source to make sure nothing in
the dispatch modules asks GitHub for the default directly again.

Run it directly, no framework needed:

    .venv/bin/python -m control.base_branch_test
"""
import ast
import asyncio
import inspect
import pathlib
import sys
import tempfile
from pathlib import Path

from control import db, github, preflight, repos, runner
from control.config import settings

fails: list[str] = []


def check(name, got, want=True):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n       got={got!r} want={want!r}"))
    if not ok:
        fails.append(name)


def run(coro):
    return asyncio.run(coro)


ROOT = pathlib.Path(__file__).resolve().parent.parent

# ---------- no dispatch path asks GitHub for the default branch itself

for relpath in ("control/runner.py", "control/plan.py", "control/provision.py"):
    tree = ast.parse((ROOT / relpath).read_text())
    direct = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "default_branch"
    ]
    check(f"{relpath} never calls default_branch directly", direct, [])

# ---------- the register

tmp = tempfile.TemporaryDirectory()
object.__setattr__(settings, "db_path", Path(tmp.name) / "factory.db")
run(db.init())
run(repos.add("acme/api"))

asked: list[tuple] = []


async def fake_default(repo):
    asked.append(("default", repo))
    return "main"


async def fake_ref_sha(repo, ref):
    asked.append(("ref", repo, ref))
    return "abc123def456" if ref in ("main", "develop", "test") else None


github.default_branch = fake_default
github.ref_sha = fake_ref_sha

check("an unconfigured repo follows its default branch", run(repos.base_branch("acme/api")), "main")
check("and nothing is configured for it", repos.configured_base("acme/api"), None)

asked.clear()
row = run(repos.set_base_branch("acme/api", "develop"))
check("a branch that exists is stored", row.get("base_branch"), "develop")
check("it was asked about before being stored", ("ref", "acme/api", "develop") in asked, True)

asked.clear()
check("a configured repo answers its integration branch", run(repos.base_branch("acme/api")), "develop")
check("without asking GitHub for the default", [a for a in asked if a[0] == "default"], [])


def refused(branch):
    try:
        run(repos.set_base_branch("acme/api", branch))
    except ValueError as exc:
        return str(exc)
    return None


check("a branch that does not exist is refused", "no branch" in (refused("nope") or ""), True)
check("a name that is not a branch is refused before GitHub is asked",
      "not a branch name" in (refused("a..b") or ""), True)
check("a refused change leaves the stored branch alone", repos.configured_base("acme/api"), "develop")
try:
    run(repos.set_base_branch("other/x", "develop"))
    unwatched = None
except ValueError as exc:
    unwatched = str(exc)
check("an unwatched repo is refused", "not watched" in (unwatched or ""), True)

row = run(repos.set_base_branch("acme/api", ""))
check("an empty branch reverts to the default", row.get("base_branch"), None)
check("and the repo follows main again", run(repos.base_branch("acme/api")), "main")

# ---------- preflight

check("an existing integration branch passes",
      preflight.base_branch_check("develop", "abc123def456").ok, True)
missing = preflight.base_branch_check("develop", None)
check("a missing one blocks", (missing.ok, missing.fatal), (False, True))
run_src = inspect.getsource(preflight.run)
check("preflight resolves the base through the register", "repos.base_branch(repo)" in run_src, True)
check("and only checks the branch when one is configured", "repos.configured_base(repo)" in run_src, True)

# ---------- merging into a non-default branch closes the issue itself

merge_src = inspect.getsource(runner._merge)
check("a merge on a repo with an integration branch closes the issue",
      merge_src.find("repos.configured_base(repo)") < merge_src.find("github.close_issue(") and
      "github.close_issue(" in merge_src, True)

print()
print(f"{len(fails)} failed: {', '.join(fails)}" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
