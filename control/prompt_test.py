"""What the agent is told: the issue, the repo's own notes, and where the work goes.

A false fact in the prompt costs more than a missing one, because the agent acts on it before
it can find out. So the repo's own `.factory.md` reaches the agent verbatim, a repo without
one gets a default that names nothing as universal, and the pull-request step says exactly
which branch to target or which open PR to continue.

Run it directly, no framework needed:

    .venv/bin/python -m control.prompt_test
"""
import asyncio
import sys

from control import github, runner

fails: list[str] = []


def check(name, got, want=True):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n       got={got!r} want={want!r}"))
    if not ok:
        fails.append(name)


ISSUE = {"number": 7, "title": "Do the thing", "body": "## Acceptance criteria\n- [ ] it works"}
PROFILE = "## Setup\n`npm ci`\n\n- `npm test` is the whole suite."


def prompt(notes=runner.DEFAULT_PROJECT_NOTES, open_pr=None, issue=ISSUE):
    return runner.build_prompt("a/repo", issue, "factory/issue-7", "test", notes, open_pr)


flat = lambda text: " ".join(text.split())  # noqa: E731

# ---------- the issue and the profile reach the agent
p = prompt(PROFILE)
check("the issue title is in the prompt", "Issue #7: Do the thing" in p)
check("the issue body is in the prompt, verbatim", "- [ ] it works" in p)
check("the repo's own notes are in the prompt", "`npm test` is the whole suite." in p)
check("an empty body says so rather than leaving a hole",
      "(no description given)" in prompt(issue={**ISSUE, "body": ""}))

# ---------- the order of work
check("setup comes before making the change",
      p.find("Install this project's dependencies") < p.find("Make the change"))
check("setup is run in the foreground with an explicit timeout", "timeout 900" in p)
check("pushing names the run's own branch", "git push -u origin factory/issue-7" in p)
check("verification points at the issue's own section", '"How to verify"' in flat(p))
check("out-of-scope sections are respected", "out of scope" in p)

# ---------- where the work goes
new = flat(prompt())
check("a new PR targets the run's base branch", "gh pr create --base test --fill" in new)
check("and links the issue", '"Closes #7"' in new)
check("and says nothing about an existing PR", "already open" in new, False)

cont = flat(prompt(open_pr="https://github.com/a/repo/pull/12"))
check("an open PR is continued, not duplicated", "do not open a second pull request" in cont)
check("its comments are read first", "gh pr view https://github.com/a/repo/pull/12 --comments" in cont)
check("and no new PR is requested", "gh pr create" in cont, False)

# ---------- invariants of every run
for invariant in ("in the foreground", "open a draft pull request", "Only what you pushed survives"):
    check(f"still says {invariant!r}", invariant in flat(p))

# ---------- a repo with no profile
d = flat(prompt())
for lock, install in (
    ("package-lock.json", "npm ci"),
    ("uv.lock", "uv sync --frozen"),
    ("Cargo.lock", "cargo fetch"),
):
    check(f"the default names {install!r} only as what {lock} implies", f"`{lock}` -> `{install}`" in d)
check("the default points at the repo's own rules files", "CLAUDE.md" in d)


# ---------- fetching: a missing profile is an answer, an outage is not a failure
async def fetch(stub):
    real, github.file = github.file, stub
    try:
        return await runner.project_notes("a/repo", "test")
    finally:
        github.file = real


async def missing(repo, path, ref):
    return None


async def blank(repo, path, ref):
    return "   \n"


async def outage(repo, path, ref):
    raise RuntimeError("502")


async def found(repo, path, ref):
    return f"# {repo}:{path}@{ref}\n{PROFILE}\n"


check("no .factory.md -> the default", asyncio.run(fetch(missing)), runner.DEFAULT_PROJECT_NOTES)
check("an empty .factory.md -> the default", asyncio.run(fetch(blank)), runner.DEFAULT_PROJECT_NOTES)
check("GitHub down -> the default, not a dead run",
      asyncio.run(fetch(outage)), runner.DEFAULT_PROJECT_NOTES)
check("a profile is read from the run's base branch",
      asyncio.run(fetch(found)).startswith("# a/repo:.factory.md@test"))

print()
print(f"{len(fails)} failed" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
