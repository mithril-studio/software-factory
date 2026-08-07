# skills

Skills loaded by the coding agent running inside a boxd VM.

A skill is a markdown file of instructions the agent reads at session start. Skills contain
no code that runs on the control plane and no LLM calls of their own — they tell the agent
how to behave. They are installed into the golden VM image at `~/.claude/skills/<name>/`.

## Skills

| Skill | Status | Purpose |
|---|---|---|
| [`memory`](./memory) | V0 | Read accumulated learnings at start; append new ones before finishing |

## Why memory is a skill and not a service

The full design for a memory engine — CLI, deterministic retrieval, hygiene, supersession —
is specified in `../../memory-engine-spec.md` and will become its own standalone repo. It is
deliberately **not** built yet.

The spec has six open decisions (§14) that cannot be answered from a chair: domain
granularity, per-repo vs. central storage, whether agents may record autonomously. Running
the format as a skill first produces real records inside real pull requests, where they can
be reviewed. Those records are the evidence that answers the open questions.

The storage format in `memory/SKILL.md` §3 is intentionally identical to the spec's, so the
extracted binary reads everything written during V0 without migration.

## Installing into a golden VM

```bash
boxd machine cp -r ./memory <golden>:/home/boxd/.claude/skills/memory
```

Re-snapshot the golden after changing a skill; running forks do not pick up changes.
