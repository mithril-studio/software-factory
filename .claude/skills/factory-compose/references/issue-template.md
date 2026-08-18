# Issue body template

Every generated issue body follows this exact structure. The **title** is a concise imperative
(e.g. "Add Postgres schema and migrations for runs"), passed separately — not part of the body.

```markdown
<!-- factory-compose: <slug> step <n>/<total> -->

## Objective
<2–4 sentences: what this step delivers, and *why* it matters to the project. Written so an
agent with zero prior context and only this issue can orient. Name which earlier steps it
builds on by role, e.g. "builds on the schema from the previous step".>

## Task
<The concrete change to make, as a few bullet points. Imperative and specific; file- or
module-level where known. This is the heart of the prompt the building agent receives.>

## Where this goes
<Grounding line — see rules. Then the files this step is expected to touch:>
- `path/to/file.ts` — new: <what it holds>
- `path/to/other.ts` — extend: <what changes>
- `tests/integration/thing.test.ts` — new: proves AC1, AC2

If the repo disagrees with a path here, follow the repo and say so in the pull request.

## Acceptance criteria
```yaml
- id: AC1
  mode: test
  statement: "<what must be true, in one sentence, as a fact about behaviour>"
  verify: "<path to the test file that proves it>"
- id: AC2
  mode: structure
  statement: "<...>"
  verify: "<a shell command whose exit status decides>"
```

## Boundaries
- **Always:** <invariants specific to this step that must hold throughout.>
- **Stop and flag:** <conditions under which the agent should stop rather than guess — push
  what it has and open a draft PR explaining what blocked it.>
- **Never:** <what NOT to do here — the work later steps own. This bounds the agent's run.>

## Sequence
Step <n> of <total> in the "<slug>" backlog. Depends on: PENDING
Do not depend on any work not listed above; later steps handle the rest.
```

## Rules

- **The first line MUST be the marker** `<!-- factory-compose: <slug> step <n>/<total> -->`. It is
  invisible on GitHub and is both the idempotency key and a provenance stamp. Use the same
  `<slug>` for every issue in one backlog.
- **Leave the dependency line ending in the literal token `Depends on: PENDING`.** The creation
  script replaces `PENDING` with the real prior issue number(s) once they exist (or
  "nothing (foundation)" for step 1). Real numbers aren't known until `gh issue create` returns
  them.
- **Do not put `agent:queued` in the body.** It is applied as a label at creation time.

## `## Objective` — what and why

The `what` orients the agent. The `why` is what lets it make a sane call when the task turns out
to be underspecified — an agent that knows the purpose of a step picks the interpretation that
serves it, and one that only knows the instruction picks whichever is easiest.

Name predecessors **by role, not by number** ("the schema from the previous step"). Issue numbers
aren't known when you draft, and an agent reading `#14` cannot see `#14`.

## `## Where this goes` — the file map

This section exists because the building agent otherwise spends a large share of its run — and
its context budget — grepping around to work out where things belong. Naming the files removes
that phase. It also sharpens a gate that already exists: the reviewing agent maps every changed
file to a criterion or to the stated task and reports the leftovers as scope creep. A file map
makes that mapping concrete instead of inferred.

**A guessed path is worse than no path.** It misleads the builder into creating a parallel
structure the repo doesn't use, and it makes the reviewer's scope map wrong in both directions.
So:

- **Existing repo — ground it.** Read the actual tree before writing this section, and open it
  with the grounding line: `Grounded in the repo at <branch>@<short-sha>.` Look at where tests
  live, how modules are named, what the existing layout implies. If you did not look, do not
  write this section.
- **New repo — say you are setting the convention.** Step 1 opens with
  `Establishes the layout; later steps follow it.`, and later steps with
  `Follows the layout established in step 1.` Here the paths are a decision, not an
  observation, and that is exactly the value: every later issue inherits one layout instead of
  each agent inventing its own.
- **If you genuinely don't know, omit the whole section.** Silence costs the agent some grepping.
  A wrong map costs a bad PR.
- **Mark every entry `new:` or `extend:`.** They are different instructions.
- **Test paths here must match the `verify:` paths exactly**, and say which criteria they prove.
  It is one fact; state it once and keep the two sections consistent.
- **Advisory, not binding.** The escape-hatch line is load-bearing — the repo is the truth, this
  is a well-informed starting point. Without it a stale path becomes an order.

## `## Boundaries` — three lanes

Replaces the older `Out of scope` heading, which was only the `Never` lane.

- **Always** — invariants that must hold throughout this step. Keep them **specific to this
  issue**. If a rule is true for every issue in every repo (commit as you go, stay in scope), it
  already lives in the builder's prompt or the repo's `CLAUDE.md`; repeating it here is noise
  that dilutes the lines that are actually specific.
- **Stop and flag** — the autonomous translation of "ask first". There is no human in the VM and
  the VM is destroyed at the end of the run, so an agent cannot ask anything. What it can do is
  push what it has and open a draft PR saying what blocked it. Use this lane for the cases where
  guessing is worse than stopping: a schema change that would lose data, an ambiguity with two
  plausible readings that are expensive to reverse, a missing credential.
- **Never** — the old `Out of scope`, and still the primary defense against an over-scoped run.
  It tells the agent where its lane ends.

## `## Acceptance criteria` — the success contract

**This is the success-criteria section.** It is deliberately a YAML block rather than prose,
because two different agents read it: the one building the change, and the one reviewing the PR
afterwards with no human involved. A criterion an agent has to *judge* is a criterion it can
rationalise; a criterion it has to *execute* is one it cannot. So each carries a `mode` saying
how it gets checked, and the reviewer runs that check rather than forming an opinion.

### The four modes

Pick the mode when you write the criterion. The reviewing agent executes the mode it is given
— it never chooses how hard to look, because given the choice it would choose the cheapest,
which is reading the diff and reasoning about it. That is where hallucination lives.

| mode | `verify` holds | how the reviewer checks it | blocks a merge? |
|---|---|---|---|
| `test` | path to a test file, or `path::test name` | runs it; must pass on the branch **and fail without the change** | yes |
| `probe` | a shell command | runs it; exit status decides | yes |
| `structure` | a shell command, usually grep/find | runs it; exit status decides | yes |
| `inspect` | path(s) a human would read | reports what it found | **no — advisory only** |

**`test` is the default. Reach for anything else only when a test genuinely cannot express
the criterion.**

### Why `test` criteria must also fail without the change

The reviewer checks out the base commit, puts the branch's new test files onto it, and runs
them. They have to fail there. If a new test passes against code written before the change,
one of two things is true: the criterion was already satisfied and the agent changed nothing,
or the test asserts nothing at all. Neither is visible from reading a diff, and both are
exactly how an agent reports success it did not earn.

If a criterion is deliberately about *preserving* existing behaviour, mark it
`regression: true` and the reviewer skips that check for it.

### A malformed criterion is worse than a missing one

The control plane parses this block itself (`control/runner.py:parse_criteria`) and is
deliberately unforgiving in one direction: anything it cannot read, it drops silently.

- A YAML syntax error anywhere in the block → the **whole block** parses to nothing → review is
  **skipped entirely** and the PR merges with no gate at all.
- A single criterion missing `id`, `mode` or `statement`, or carrying an unrecognised `mode` →
  **that criterion is dropped** and the others still run. The reviewer then approves against a
  fraction of the contract while reporting success.

Neither shows up as an error anywhere. The issue renders perfectly on GitHub. This is why the
rules below are mechanical rather than stylistic, and why the creation script validates the
block before any issue is created.

### Rules

- **Every issue needs at least one criterion that can block.** An issue whose criteria are all
  `inspect` cannot be verified by anything, and will be waved through.
- **`statement` is a fact about behaviour, not a task.** "Login is limited to 5 attempts per 15
  minutes per account", not "add rate limiting to login". The task belongs in `## Task`; this
  is the thing that must be *true afterwards*.
- **One criterion, one fact.** If it contains "and", it is probably two criteria.
- **Do not add a "tests pass and the build is green" criterion.** CI enforces that on every PR
  and the reviewer runs the checks itself. It was noise in every issue.
- **`verify` must work in a bare VM** — no secrets, no external network, no human. A criterion
  that needs either fails forever and halts the repo. If the criterion is worth having, find a
  VM-reachable way to check it; if there isn't one, make it `inspect` and say why in the
  statement.
- **Write `verify` paths for tests that do not exist yet.** You are naming where the proof will
  live; the building agent creates it. Keep them identical to the test entries in
  `## Where this goes`.
- **Always quote `statement` and `verify`.** Both routinely contain characters YAML treats as
  syntax — a colon in prose, a `{` in a JSON body, a `#`, a leading `!`. Unquoted, the block
  stops parsing and the reviewer has no criteria to run. This is not hypothetical: the first
  draft of the example below used an unquoted `{"ok": true}` and failed to parse. Quote
  everything and the whole class of problem disappears.
- **Validation is enforced, not suggested.** `scripts/create_backlog.sh` runs
  `scripts/validate_backlog.py` over every drafted file and refuses to create anything if a
  block is malformed, a criterion is incomplete, or an issue has no blocking criterion. Run it
  yourself while drafting:
  ```bash
  python3 scripts/validate_backlog.py <slug> <workdir>
  ```

### Worked example

```yaml
- id: AC1
  mode: test
  statement: "A sixth sign-in attempt within 15 minutes is rejected with 429."
  verify: "tests/integration/auth-throttle.test.ts"
- id: AC2
  mode: test
  statement: "The counter is per account, so one user's failures never lock out another."
  verify: "tests/integration/auth-throttle.test.ts::isolates accounts"
- id: AC3
  mode: structure
  statement: "No route handler reads the throttle table directly; all go through the repository."
  verify: "! grep -rn 'from(\"login_attempts\")' src/app/"
- id: AC4
  mode: inspect
  statement: "docs/security.md records the chosen thresholds and why."
  verify: "docs/security.md"
```

## Title guidance

- Imperative, specific, and self-contained: "Add the /shorten endpoint with validation", not
  "endpoint work".
- No step numbers in the title (the sequence lives in the body and in the issue number).
