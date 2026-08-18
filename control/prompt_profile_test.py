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
# The default used to name no build tool at all, because naming one project's tool as
# universal is the bug this file exists for. It names several now — but each is the answer to
# a question about the checkout ("which lock file is at the root?"), never an assertion that
# this repo uses it, so the agent still finds out rather than being told wrong.
for lock, install in (
    ("package-lock.json", "npm ci"),
    ("uv.lock", "uv sync --frozen"),
    ("Cargo.lock", "cargo fetch"),
):
    check(f"the default names {install!r} only as what {lock} implies",
          f"`{lock}` -> `{install}`" in " ".join(d.split()), True)
check("the default points at the repo's own rules files", "CLAUDE.md" in d, True)

# ---------- setup step  (AC1)
# The prompt used to tell the agent the machine was already set up for this project and that
# setup work was therefore wasted work. On a repo-agnostic golden that is false, and a false
# fact costs more than a missing one: the agent acts on it, then spends turns discovering it
# needs an install and guessing the command, and every one of those turns re-reads the whole
# context. Both prompts get the step — a repo with a profile and a repo without one.
for kind, built in (("with a profile", p), ("without one", d)):
    flat = " ".join(built.split())
    where_install = flat.find("Install this project's dependencies")
    where_change = flat.find("Make the change")
    check(f"setup step: {kind}, the prompt has one", where_install != -1, True)
    check(f"setup step: {kind}, it comes before making the change",
          -1 < where_install < where_change, True)
    check(f"setup step: {kind}, it points at the project notes for the command",
          'named under "This project"' in flat, True)
    check(f"setup step: {kind}, in the foreground with an explicit timeout",
          "in the foreground, with an explicit timeout" in flat, True)
    check(f"setup step: {kind}, and only once", "Run it **once**" in flat, True)
    check(f"setup step: {kind}, warm now means the download caches, not this repo",
          "download caches on this machine are warm" in flat, True)
    check(f"setup step: {kind}, nothing is claimed to be installed already",
          "nothing is installed for this repo yet" in flat, True)
    # Spelled in halves on purpose: the structural criterion for this change is a grep for
    # that first phrase over `control/`, and a test that quotes it would be the only hit.
    check(f"setup step: {kind}, and the old promise is gone",
          any(claim in flat for claim in
              ("Dependencies are " + "installed", "the machine is already set up",
               "setup work is wasted work")),
          False)

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
