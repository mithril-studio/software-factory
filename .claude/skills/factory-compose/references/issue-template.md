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
- [ ] <checkable and VM-verifiable — a command runs, a test passes, an endpoint responds>
- [ ] <...>
- [ ] Existing tests still pass and the build is green.

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
- **`Acceptance criteria` must be VM-verifiable.** The agent has no human to ask inside the VM;
  these are how it knows it's done and how a reviewer reads the PR. Always include the
  "existing tests still pass / build is green" line.
- **Leave the dependency line ending in the literal token `Depends on: PENDING`.** The creation
  script replaces `PENDING` with the real prior issue number(s) once they exist (or
  "nothing (foundation)" for step 1). Real numbers aren't known until `gh issue create` returns
  them.
- **Do not put `agent:queued` in the body.** It is applied as a label at creation time.

## Title guidance

- Imperative, specific, and self-contained: "Add the /shorten endpoint with validation", not
  "endpoint work".
- No step numbers in the title (the sequence lives in the body and in the issue number).
