---
name: factory-compose
description: >-
  Turn a project brief into an ordered backlog of dependency-sequenced GitHub issues for the
  Software Factory, each labeled agent:queued so the factory builds them lowest-number-first.
  Drafts the full ordered issue list from a brief plus a target owner/repo, shows it for human
  review and editing, and only creates issues after explicit approval. Use when the user says
  "compose a backlog", "factory-compose", "plan issues for the factory", "decompose this brief
  into issues", or hands over a project brief/spec together with a target owner/repo.
---

# factory-compose

Turn a **project brief** into an **ordered backlog of GitHub issues** the Software Factory can
build. You (Claude) do the thinking — decomposition and wording. A human reviews and approves.
A small script does the mechanical, order-critical creation.

## Why order and quality matter (read this first)

The factory polls a repo for issues labeled `agent:queued`, runs the **lowest-numbered** open
one on a VM, opens a PR, then moves to the next. It runs **one issue per repo at a time**, so
**issue-number order is execution order** — no dependency graph exists or is needed. If an issue
permanently fails (no PR after 3 attempts) it gets `agent:failed` and the poller **halts the
whole repo** until a human clears it. Consequences you must design around:

- **Issue #1 is the highest-risk issue** — its failure halts everything after it. Make it the
  thinnest, most mechanical, least ambiguous step (scaffolding / a green build).
- **The building agent's entire prompt is the issue title + body.** Vague issue → bad PR →
  failed run → halted repo. Body quality is the whole game.
- Create issues **in dependency order** so their numbers ascend with the sequence.

## Five hard rules

1. **Never create issues before explicit human approval.** You are a drafting tool with a
   mandatory review gate. Draft in chat; create on GitHub only after a clear yes.
2. **Idempotent.** Never duplicate a prior compose. Each body carries a hidden marker
   `<!-- factory-compose: <slug> step n/total -->`; check for it before creating.
3. **Halt-aware.** Scrutinize issue #1 hardest, and flag any acceptance criterion that needs
   secrets or networks a VM can't reach — those fail forever and halt the repo.
4. **Acceptance criteria are executable, not prose.** They are a YAML block where each
   criterion carries a `mode` (`test` / `probe` / `structure` / `inspect`) and a `verify`
   naming what proves it. Nobody reads these PRs before they merge — a reviewing agent runs
   the criteria instead, and it can only run what you made runnable. A malformed block is
   worse than a missing one: the control plane drops what it cannot parse and skips review
   silently, so the PR merges with no gate at all. `scripts/validate_backlog.py` is what
   stands between a typo and an ungated merge, and `create_backlog.sh` runs it before
   creating anything. See `references/issue-template.md`.
5. **Never invent a file path.** `## Where this goes` is only worth having when it is grounded
   in a repo you actually read. A guessed path sends the building agent off to build a
   structure the repo does not use, and it corrupts the reviewer's scope-creep check. Read the
   tree, or omit the section.

## Procedure

### 1. Intake
- Get the **brief** (inline text, pasted spec, or a file path — `Read` it) and the target
  **`owner/repo`**. If the repo is missing, ask for it. Derive a short **slug** from the brief
  (kebab-case, e.g. `url-shortener`).
- **Refuse a too-vague brief.** If it can't yield acceptance-testable issues, ask targeted
  questions (stack/language, what "done" means, key constraints) *before* drafting. A vague
  brief is the root cause of failed runs.
- **Ground yourself in the repo, if it already has code.** Read the tree, the test layout, the
  scripts a `probe` could call, `CLAUDE.md`, and `.mem/` if it exists. This is what makes
  `verify:` paths and `## Where this goes` real rather than plausible — the difference between
  a file map that saves the building agent a third of its run and one that misdirects it. Note
  the branch and short sha you read; each file map is stamped with it.

### 2. Decompose
Read `references/decomposition.md` and apply it. Produce a **linear, ordered** backlog where each
issue is one PR-sized unit an autonomous agent can finish in one VM run against the *merged*
result of all prior issues. Aim for ~5–15 issues (state your granularity and why). Every issue
body follows `references/issue-template.md` exactly — `## Objective` (what and why), `## Task`,
`## Where this goes` (the grounded file map), `## Acceptance criteria`, `## Boundaries`
(always / stop and flag / never), `## Sequence`. See `references/examples.md` for worked
brief→backlog examples.

### 3. Review in chat (not on GitHub)
Present:
- A numbered table: `# | Title | Depends on | one-line scope`.
- Then the **full body of each issue**.
- A short rationale: chosen granularity, why this order, dependencies/assumptions surfaced.

- **Open questions** — anything still unresolved. This is the only place they may exist: an
  open question left inside an issue body is an ambiguity handed to an agent alone in a VM,
  which is how repos get halted. Resolve every one here; do not proceed to step 4 while any
  remain.

Invite the human to reorder, split, merge, rescope, or edit any title/body/criterion. **Re-render
the whole list after each change** so they always see the final state.

### 4. Approve
Ask directly: **"Create these N issues in `owner/repo` now? They run in this order, #1 first."**
Offer to show the exact `gh` commands as a dry run first. Proceed only on a clear yes.

### 5. Create (the script does this deterministically)
Write each issue to `<workdir>/NN.md` (zero-padded, in order) in this file format:

```
TITLE: <the issue title>

<full issue body, starting with the marker comment, per issue-template.md>
```

Then run:

```bash
bash scripts/create_backlog.sh <owner/repo> <slug> <workdir>
```

Validate while drafting, not only at creation time:

```bash
python3 scripts/validate_backlog.py <slug> <workdir>
```

The script: validates auth + repo (fails early), ensures the five `agent:*` labels exist with
the exact colors the control plane uses, **aborts if the slug's marker already exists**
(idempotency), **runs `validate_backlog.py` over every drafted file and refuses to create
anything if one is malformed**, creates issues in `NN` order capturing their numbers,
back-fills `Depends on: #N`, and prints a report. It **stops on the first failure** (a gap is worse than a short backlog).

If the script aborts because a backlog for this slug already exists, do NOT force-duplicate.
Handle it by talking to the human: create only the genuinely missing steps, or update existing
bodies in place with `gh issue edit <n> --repo <repo> --body-file <file>`.

### 6. Report back
Give the human: links to the created issues, which runs first (#lowest) and the full order, and
two reminders — the target repo must be in `FACTORY_REPOS` (and `FACTORY_POLL=1`) for the poller
to pick them up, and a permanent failure on any issue halts the repo until cleared.

## Scope (V0)

Linear sequence only — no dependency graphs. This skill creates issues; it does not touch factory
config (`FACTORY_REPOS`), enable polling, watch runs, or auto-approve. One brief → one repo → one
ordered list. Install globally by copying or symlinking this directory into `~/.claude/skills/`.
