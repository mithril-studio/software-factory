"""What the dispatch prompt says about a project, and which machines the factory owns.

Both are things that were true of one repo and quietly assumed of all of them.

The bug this pins down: the prompt used to state one repo's setup as universal — `npm run
test:integration` at the repo root, a seeded test database, a read-only `.env`. Every one of
those is false on the second repo the factory watches, and a false fact costs more than a
missing one, because the agent acts on it before it can find out.

Run it directly, no framework needed:

    .venv/bin/python -m control.prompt_profile_test
"""
import asyncio
import sys

from control import github, runner

fails: list[str] = []


def check(name, got, want):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}\n       got={got!r}")
    if not ok:
        fails.append(name)


ISSUE = {"number": 7, "title": "Do the thing", "body": "Please."}
PROFILE = "- Commands run from `app/`.\n- `npm test` is the whole suite."


def prompt(notes: str) -> str:
    return runner.build_prompt("a/repo", ISSUE, "factory/issue-7", "main", notes)


# ---------- the profile reaches the agent
p = prompt(PROFILE)
check("the repo's own notes are in the prompt", "Commands run from `app/`." in p, True)
check("no other project's commands come with them", "test:integration" in p, False)
check("no other project's environment claims either",
      any(s in p for s in ("test database", "node_modules", "npm ci")), False)

# ---------- harness invariants survive, because they are not repo know-how
for invariant in ("foreground", "gh pr create", "git push -u origin", "memory"):
    check(f"still says {invariant!r}", invariant in p, True)

# ---------- a repo with no profile
d = prompt(runner.DEFAULT_PROJECT_NOTES)
check("the default names no build tool", "npm" in d, False)
check("the default points at the repo's own rules files", "CLAUDE.md" in d, True)

# ---------- the retry context still lands after the notes, not inside them
r = runner.build_prompt(
    "a/repo", ISSUE, "factory/issue-7", "main", PROFILE,
    attempt=2, prior_error="boom", prior_log="tail",
)
check("a retry passes attempt, not notes, as the attempt", "attempt 2 of" in r, True)
check("the previous failure is carried in", "boom" in r, True)

# ---------- fetching: a missing profile is an answer, an outage is not a failure
async def fetch(stub):
    real, github.file = github.file, stub
    try:
        return await runner.project_notes("a/repo", "main")
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
check("a profile is read from the base branch",
      asyncio.run(fetch(found)).startswith("# a/repo:.factory.md@main"), True)

# ---------- the reviewer is told the same thing, and its template still formats
REVIEW_BODY = (
    "## Objective\nShorten urls, so links fit in a tweet.\n\n"
    "## Where this goes\n- `src/shorten.ts` — new: the endpoint\n\n"
    "## Boundaries\n- **Never:** touch the schema.\n"
)
rev = runner.REVIEW_PROMPT_TEMPLATE.format(
    project_notes=PROFILE, repo="a/repo", number=7, title="t", pr_url="u",
    branch="b", base="main", base_sha="deadbeef", criteria="- id: AC1\n",
    body=REVIEW_BODY,
)
check("the reviewer gets the project's own checks", "npm test` is the whole suite" in rev, True)
check("and not another project's", "test:integration" in rev, False)

# The reviewer was told to map every changed file against "the issue's stated task" while being
# handed only the title and the criteria — the task, the file map and the boundaries were all in
# a body it never saw. It gets the body now, subordinate to the criteria.
check("the issue body reaches the reviewer", "**Never:** touch the schema." in rev, True)
check("including the file map it checks scope against", "`src/shorten.ts`" in rev, True)
check("the body is context, not a second contract",
      "do not mine it for extra gates" in rev, True)
check("a criterion is still the only thing that blocks on its own",
      "prose in the body is not one" in rev, True)

# ---------- which machines the factory owns
# The leak this pins down: reconcile swept `run-` only, so an orphaned review VM survived on
# its self-destruct timer alone and showed up in the fleet view as a golden.
check("a build VM is ours", runner.is_run_vm("run-1a2b3c4d"), True)
check("a review VM is ours too", runner.is_run_vm("rev-1a2b3c4d"), True)
check("a golden is not", runner.is_run_vm("legal-ai-golden"), False)
check("nor is the control plane", runner.is_run_vm("software-factory"), False)

print()
print(f"{len(fails)} failed" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
