# Issue body template

Every generated issue body follows this exact structure. The **title** is a concise imperative
(e.g. "Add Postgres schema and migrations for runs"), passed separately — not part of the body.

```markdown
<!-- factory-compose: <slug> step <n>/<total> -->

## Context
<1–3 sentences. What this project is, and what this step contributes. Written so an agent with
zero prior context and only this issue can orient. Name which earlier steps it builds on by role,
e.g. "builds on the schema from the previous step".>

## Task
<The concrete change to make, as a few bullet points. Imperative and specific; file- or
module-level where known. This is the heart of the prompt the building agent receives.>

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

## Out of scope
- <what NOT to do here — the work later steps own. This bounds the agent's run.>

## Sequence
Step <n> of <total> in the "<slug>" backlog. Depends on: PENDING
Do not depend on any work not listed above; later steps handle the rest.
```

## Rules

- **The first line MUST be the marker** `<!-- factory-compose: <slug> step <n>/<total> -->`. It is
  invisible on GitHub and is both the idempotency key and a provenance stamp. Use the same
  `<slug>` for every issue in one backlog.
- **`Out of scope` is load-bearing.** It is the primary defense against an over-scoped run —
  it tells the agent where its lane ends.
- **`Acceptance criteria` is a YAML block, and it is a contract.** Two different agents read
  it: the one building the change, and the one reviewing the PR afterwards with no human
  involved. A criterion an agent has to *judge* is a criterion it can rationalise; a criterion
  it has to *execute* is one it cannot. So each carries a `mode` saying how it gets checked,
  and the reviewer runs that check rather than forming an opinion. Full rules below.
- **Leave the dependency line ending in the literal token `Depends on: PENDING`.** The creation
  script replaces `PENDING` with the real prior issue number(s) once they exist (or
  "nothing (foundation)" for step 1). Real numbers aren't known until `gh issue create` returns
  them.
- **Do not put `agent:queued` in the body.** It is applied as a label at creation time.

## Acceptance criteria: the four modes

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
  live; the building agent creates it.
- **Always quote `statement` and `verify`.** Both routinely contain characters YAML treats as
  syntax — a colon in prose, a `{` in a JSON body, a `#`, a leading `!`. Unquoted, the block
  stops parsing and the reviewer has no criteria to run. This is not hypothetical: the first
  draft of the example below used an unquoted `{"ok": true}` and failed to parse. Quote
  everything and the whole class of problem disappears.
- **Validate the block before creating the issue**, with the same one-liner the control plane
  uses:
  ```bash
  python3 -c "import yaml,sys; d=yaml.safe_load(sys.stdin.read()); \
    assert all({'id','mode','statement','verify'} <= set(c) for c in d); \
    assert all(c['mode'] in ('test','probe','structure','inspect') for c in d); \
    print(f'{len(d)} criteria ok')" < block.yaml
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
