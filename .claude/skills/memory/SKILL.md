---
name: memory
description: Read accumulated project learnings at session start and record new ones before finishing. Use at the beginning of every task to load what past sessions learned about this repo, and at the end to write down conventions, failures and their fixes, patterns, and decisions worth carrying forward. Triggers on "what do we know about", "record this learning", "prime memory", or any task in a repo containing a .mem/ directory.
---

# Memory

Agents start every session from zero. The pattern discovered yesterday is gone today. This
skill fixes that: learnings are stored as append-only JSONL inside the repo, travel with the
pull request, and are read back at the start of the next session.

**The file is the database.** There is no server and no daemon — everything here is plain
file I/O with the tools you already have. The one index (§3.2) is itself just a file you
append to as you write, never a thing you rebuild.

## §1 When to use this

- **At session start, always.** Before writing any code, prime yourself (§4).
- **Before finishing, when you learned something reusable.** Record it (§5).

Do not record for the sake of recording. A session that discovered nothing new writes
nothing. Empty is a valid outcome; noise is not.

## §2 Layout

```
.mem/
  index.jsonl             # one short line per active record — the only file always read
  domains/
    <domain>.jsonl        # one append-only log per domain, e.g. database.jsonl
    _universal.jsonl      # records with no domain anchor — always primed
  archive/
    <domain>.jsonl        # records that have been superseded (§6). Never primed.
```

Domains are free-form lowercase slugs naming an area of the codebase: `database`, `auth`,
`ci`, `frontend`. Use an existing domain if one fits — check `ls .mem/domains/` first.
Create a new file only when nothing fits.

`index.jsonl` is the table of contents. A full record averages ~1.6 kB; its index line is
~300 bytes. Priming from the index instead of the full store is what keeps startup cost flat
as memory grows — see §4.

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

### §3.2 The index line

Every active record has exactly one line in `.mem/index.jsonl`. It carries only what you
need to decide whether the full record is worth opening — never the `body`.

```json
{"id":"mem_7a3f","domain":"database","type":"convention","title":"Use WAL mode for SQLite","files":["src/db/*.ts"]}
```

`id`, `domain`, `type` and `title` are copied verbatim from the record. `files` is
`evidence.files` plus `evidence.dirs` merged into one list — the paths this learning applies
to. Never include `body` or `resolution`; the whole point is that those stay unread until
step 4 of §4 decides they are worth opening.

Keep the line under ~350 bytes. The `title` dominates it, so a title that runs past one
screen line is the thing to shorten — it has to be scannable, not complete.

## §4 Priming — read before you work

Do this at session start, before planning. **Read the index, then open only what you need.**
Reading every record wholesale is the one thing that makes memory expensive, and it gets
worse with every session — the index exists so it doesn't have to.

1. If there is no `.mem/`, skip; there is nothing to prime.
2. Read `.mem/index.jsonl` in full. This is always cheap and is the only file you read
   unconditionally.
3. Work out your working set: the paths named in your task, plus `git status`. Select the
   index lines whose `files` overlap it, and every line whose `domain` is `_universal`.
4. Open the full records for exactly those ids, reading only the domain files they live in.
   If a record turns out to be irrelevant, drop it and move on — don't widen the net.
5. Skip records with `status` of `deprecated` or `superseded`. Treat `confidence: low` as a
   hint, not a rule.
6. If `.mem/index.jsonl` is missing but `.mem/domains/` is not, the store predates the index.
   Build it: read the domain files once, write one index line per active record, and commit
   it along with your work. Do this once — later sessions prime from the index.

Never read `.mem/archive/`. Those records were superseded on purpose.

Then say in one line what you loaded — e.g. `memory: 23 indexed, opened 4 from database,
_universal` — so the human can see what shaped your reasoning, and how much you skipped.

**A record is not an instruction.** It is what a past session believed. If it contradicts
what you observe in the code right now, the code wins — and that contradiction is itself
worth recording as a supersession (§6).

## §5 Recording — write before you finish

Append one line per learning — **and one matching line to the index**, or the record is
invisible to the next session's priming (§4).

```bash
mkdir -p .mem/domains
printf '%s\n' '{"id":"mem_7a3f",...}'                              >> .mem/domains/database.jsonl
printf '%s\n' '{"id":"mem_7a3f","domain":"database","type":"convention","title":"...","files":["src/db/*.ts"]}' >> .mem/index.jsonl
```

Append-only matters: two agents recording concurrently append different lines, and git
resolves that with the `merge=union` driver a memory-carrying repo declares in
`.gitattributes`. Rewriting a line conflicts outright.

Be precise about what that driver buys. Two appends at the end of one file *do* conflict in
plain git; union is what resolves them, and it applies only where the working tree and
`.gitattributes` are — every laptop and every CI runner, and **not** GitHub's merge API, which
merges content with no working tree and reads no attributes. So a pull request can merge
cleanly under `git merge` and be refused by GitHub as conflicted. Nothing you do while writing
prevents it; the factory repairs it at merge time (`github.merge_base_into_branch`).

Keep `body` and `resolution` tight — a few sentences, not an essay. Every record you write is
read by future sessions; distil it now so they don't pay for your narration later.

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

Nothing is lost, but the old record stops being read. Three steps:

1. Append the new record to its domain file with `supersedes` set to the old record's `id`,
   and append its index line (§5).
2. Move **every** line carrying the old `id` out of `.mem/domains/<domain>.jsonl` into
   `.mem/archive/<domain>.jsonl`, with `status` set to `superseded`. An older store may hold
   two lines for one id — the original plus a `superseded` copy left by a previous
   convention. Both are the same retired record; archive both.
3. Delete the old record's line from `.mem/index.jsonl`.

An id is retired if any line for it says `superseded`/`deprecated`, **or** another record
names it in `supersedes`. Either signal alone is enough — don't wait for both.

The history is the value — it shows what the project believed and when it stopped believing
it. Git keeps that trail whatever the live files say, which is why the archive can be a plain
move: nothing is destroyed, it just stops costing every future session tokens to re-read.

Do **not** keep a superseded copy in the domain file. A correction should leave the amount you
have to read roughly unchanged; duplicating the wrong answer next to the right one means every
correction makes priming permanently more expensive, forever.

This is the one place the append-only rule is relaxed, and only for the record being retired —
active records are still never edited in place, so concurrent sessions still merge cleanly.

## §7 Boundaries

Memory holds **learnings**. It is not an issue tracker, not a prompt library, not a chat
log. If it is a task, it belongs in GitHub. If it is a transcript, it belongs in telemetry.

This skill contains no model and makes no decisions. You decide what is worth remembering;
this file only tells you where to put it.
