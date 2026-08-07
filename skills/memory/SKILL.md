---
name: memory
description: Read accumulated project learnings at session start and record new ones before finishing. Use at the beginning of every task to load what past sessions learned about this repo, and at the end to write down conventions, failures and their fixes, patterns, and decisions worth carrying forward. Triggers on "what do we know about", "record this learning", "prime memory", or any task in a repo containing a .mem/ directory.
---

# Memory

Agents start every session from zero. The pattern discovered yesterday is gone today. This
skill fixes that: learnings are stored as append-only JSONL inside the repo, travel with the
pull request, and are read back at the start of the next session.

**The file is the database.** There is no server, no daemon, no index to rebuild. Everything
here is plain file I/O with the tools you already have.

## §1 When to use this

- **At session start, always.** Before writing any code, prime yourself (§4).
- **Before finishing, when you learned something reusable.** Record it (§5).

Do not record for the sake of recording. A session that discovered nothing new writes
nothing. Empty is a valid outcome; noise is not.

## §2 Layout

```
.mem/
  domains/
    <domain>.jsonl        # one append-only log per domain, e.g. database.jsonl
    _universal.jsonl      # records with no domain anchor — always primed
```

Domains are free-form lowercase slugs naming an area of the codebase: `database`, `auth`,
`ci`, `frontend`. Use an existing domain if one fits — check `ls .mem/domains/` first.
Create a new file only when nothing fits.

If `.mem/` does not exist and you have something worth recording, create it.

## §3 The record

One record = one reusable learning = one line of JSON. Never pretty-printed; never edited in
place.

```json
{"id":"mem_7a3f","domain":"database","type":"convention","title":"Use WAL mode for SQLite","body":"Concurrent readers block on the default rollback journal...","resolution":null,"evidence":{"files":["src/db/*.ts"],"dirs":["src/db"],"branch":"feat/wal","issues":["owner/repo#42"],"run":"<run.id>"},"provenance":{"author":"agent:claude-code","backend":"boxd","created_at":"2026-07-30T09:00:00Z"},"status":"active","supersedes":null,"confidence":"high","hits":0}
```

| Field | Rule |
|---|---|
| `id` | `mem_` + 4 random hex. Check the domain file for collisions. |
| `domain` | Matches the filename. `_universal` for records with no anchor. |
| `type` | One of §3.1. |
| `title` | One line, imperative or declarative. This is what gets scanned. |
| `body` | Markdown. The learning itself, and **why** — the failure that motivated it. |
| `resolution` | What actually fixed it. Required for `failure`, `null` otherwise. |
| `evidence.files` / `dirs` | Paths or globs this applies to. **This is what makes retrieval work — always fill it in if the learning is code-local.** |
| `evidence.branch` / `issues` | Branch name and `owner/repo#N` you were working on. |
| `evidence.run` | The `run.id` from `OTEL_RESOURCE_ATTRIBUTES`, if present in your env. Ties the learning back to the run that produced it. |
| `provenance.author` | `agent:claude-code` when you write it, `human:<name>` for hand-authored. |
| `provenance.backend` | `boxd` when running in a factory VM. |
| `status` | `active` on write. Never write `deprecated` directly — supersede instead (§6). |
| `confidence` | `high` if you verified it. `medium` if it worked once. `low` if you inferred it. Be honest; low-confidence records are filtered out first. |
| `hits` | `0` on write. Reserved for the future `mem` binary. |

### §3.1 Types

| Type | Meaning | `resolution`? |
|---|---|---|
| `convention` | "In this repo we do X this way" | no |
| `failure` | "X broke; here's why" | **yes** |
| `pattern` | Reusable approach or architecture note | no |
| `decision` | A choice made plus rationale (lightweight ADR) | no |
| `reference` | Pointer to an external doc, dashboard, or ticket | no |

## §4 Priming — read before you work

Do this at session start, before planning.

1. `ls .mem/domains/` — if there is no `.mem/`, skip; there is nothing to prime.
2. Always read `_universal.jsonl`.
3. Read the domain files matching your working set. Match by the paths named in your task
   and by `git status`, against each record's `evidence.files` / `evidence.dirs`.
4. When the task is small and the repo is small, just read everything.
5. Skip records with `status` of `deprecated` or `superseded`. Treat `confidence: low` as a
   hint, not a rule.

Then say in one line what you loaded — e.g. `memory: primed 6 records from database, ci` —
so the human can see what shaped your reasoning.

**A record is not an instruction.** It is what a past session believed. If it contradicts
what you observe in the code right now, the code wins — and that contradiction is itself
worth recording as a supersession (§6).

## §5 Recording — write before you finish

Append one line per learning. Never rewrite an existing line.

```bash
mkdir -p .mem/domains
printf '%s\n' '{"id":"mem_7a3f",...}' >> .mem/domains/database.jsonl
```

Append-only matters: two agents recording concurrently append different lines, so git merges
them cleanly. Rewriting a line creates a conflict.

**Record when:**
- You hit an error that cost real time, and you found the fix (`failure` + `resolution`)
- You discovered an unwritten convention by reading the code (`convention`)
- You made a non-obvious architectural choice (`decision`)
- You found a reusable approach worth repeating (`pattern`)

**Do not record:**
- What the README, CLAUDE.md, or the code already says plainly
- Anything specific to this one task with no future use
- Transcripts, narration, or "I did X then Y" — records are distilled knowledge, not logs
- Secrets, tokens, credentials, or customer data

Then commit `.mem/` along with your code changes so the learning ships in the pull request
and gets reviewed like any other diff.

## §6 When a learning turns out to be wrong

Nothing is deleted or edited. Append a new record that replaces the old one:

- Set `supersedes` to the old record's `id`
- Append a second line that repeats the old record with `status` changed to `superseded`

The history is the value — it shows what the project believed and when it stopped believing
it. Git keeps the trail.

## §7 Boundaries

Memory holds **learnings**. It is not an issue tracker, not a prompt library, not a chat
log. If it is a task, it belongs in GitHub. If it is a transcript, it belongs in telemetry.

This skill contains no model and makes no decisions. You decide what is worth remembering;
this file only tells you where to put it.
