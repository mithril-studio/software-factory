# Software Factory

An agentic development system. GitHub issues go in; reviewed pull requests come out.

A control plane watches repos for work, restores an isolated boxd VM per task, runs a coding
agent inside it, and reaps the VM when done. The control plane is deterministic and contains
no LLM. All intelligence lives in the agent, inside the VM.

> **Status:** V0, started 2026-07-30. Second attempt; the first failed on scope, and the
> governing constraint of this build is *smallness*. The full loop ships: build, review,
> CI, auto-merge, with repository memory validated in CI.

---

## §1 The layers

One repo, one deployable. Each layer gets a folder so it can be split into its own repo
once it has earned an independent release cycle — build it working first, fragment later.

| Folder | Layer | Responsibility | Contains an LLM? |
|---|---|---|---|
| `control/` | Control plane | Watch issues, provision VMs, dispatch agents, review, merge, reap | No |
| `telemetry/` | Trace | Normalize agent events; store runs, token usage, and cost | No |
| `web/` | UI | React SPA, built to `web/dist` and served by `control` | No |
| `scripts/` | Operations | Goldens, deploy, the CI gate list, health monitoring | No |

`control/` rather than `exec/`: `exec` is a Python keyword and cannot name a package.

Skills are not a folder here. They live in
[agent-skills](https://github.com/mithril-studio/agent-skills) because they ship into the VM
rather than onto the server — installed globally when a golden is built, and reused by agents
that have nothing to do with this project.

Nothing in this system calls a model except the agents running inside boxd VMs. This is the
**passive-layer principle**: the tools read and write, the caller has the intelligence.

## §2 Topology

```
   Hetzner VM  (systemd, deployed by git pull)
   ┌─────────────────────────────────────────┐
   │  control (FastAPI) ──► telemetry        │
   │    │  polls GitHub        │             │
   │    └────── SQLite ────────┘             │
   └────┬────────────────────────────────────┘
        │ boxd SDK
        ▼
   ┌─────────────────────────────┐
   │  boxd VM (from a golden)    │
   │    agent + skills           │
   │    → pushes branch, opens PR│
   └─────────────────────────────┘
```

One short-lived boxd machine per run, restored from a golden snapshot in ~0.2s (`fork`
survives only as the rollback path). Goldens come in two tiers: a per-repo
`golden-<repo-slug>` with the repo already cloned and installed, falling back to the shared
`golden-copy` base. Connecting a repo kicks off a provisioning run that builds its warm
golden — `OPERATIONS.md` covers building and refreshing them.

The control plane runs on a Hetzner VM under systemd and is deployed by `git pull`; it is not
a container and it is not on boxd. Telemetry is fed in process: `control.runner._stream`
parses the agent's event log for the live view and hands the same events to a `Recorder` —
no collector sits between the VM and the database.

## §3 Contracts between layers

These are the only things the layers share. They are written down even inside one repo,
because they are what makes splitting the layers out later cheap. Change them deliberately.

### §3.1 `run.id` — the correlation key

`uuid4().hex`, minted by `control` when a run is registered — 32 hex characters, no dashes;
the VM name carries the first 8. It is the join key across every layer.

`control` injects exactly one telemetry variable into the agent VM:

```
OTEL_RESOURCE_ATTRIBUTES=run.id=<id>,issue=<owner/repo#123>,repo=<owner/repo>,vm=<box-name>
```

Review runs append `,kind=review`. That line is the entire producer contract — no OTLP
endpoint, exporter, or token is set today.

### §3.2 The adapter — the model-agnostic boundary

Runtime specifics are confined to one adapter (`telemetry/normalize.py`); no table names a
runtime. The adapter is fed in process from the event stream `control` already parses, and
from salvaged transcripts replayed after the fact. OTLP remains the intended wire format for
producers outside this process — it becomes a third caller of the same adapter rather than a
second implementation. See `../telemetry/README.md` §2 for why the transport was deferred and
the boundary built first.

The dependency runs one way: `control` imports `telemetry`, never the reverse.

### §3.3 Memory

The record schema is defined by the `memory` skill in
[agent-skills](https://github.com/mithril-studio/agent-skills). The agent writes `.mem/`
records in the target repo and they ship inside the pull request, reviewed like code. The
factory itself touches memory in three deterministic ways:

- **Validation.** `python -m control.memory validate` checks the store's shape — index,
  domains, archive, evidence paths that resolve — and runs as a CI gate via
  `scripts/verify.sh`. A malformed record fails the build.
- **The receipt.** Right after priming, the agent prints one `FACTORY_MEMORY {json}` line
  naming what it indexed and opened. `control` parses it out of the stream into `telemetry`'s
  `memory_reads` and `memory_receipts` tables, so retrieval is measurable per run and repo.
- **The candidate queue.** Learnings the agent is not confident enough to commit go to a
  JSONL file the runner harvests before reaping the VM, into the `memory_candidates` table
  for human triage. Accepting a candidate records a verdict; it never writes `.mem/` — a
  future agent does that, with the store's own tools.

### §3.4 The golden launch contract

A golden announces itself with two files: `/usr/local/bin/factory-agent` (the launch command)
and `/etc/factory/agent.json` (the manifest — agent name, transcript glob, event format).
The runner prints the manifest as a `FACTORY-MANIFEST` line, selects the telemetry adapter
from it, and falls back to `claude -p` when `factory-agent` is absent. The full contract is
`../control/README.md` §2.3.

## §4 The pipeline

Every phase of an issue is a row in `runs` with its own `kind`, and every step is a status
transition on that row:

| kind | VM | What it does |
|---|---|---|
| `build` | `run-*` | Take the issue, push a branch, open a PR |
| `review` | `rev-*` | Judge the PR against the issue's acceptance criteria; record a verdict |
| `ci` | none | Record what the PR's checks decided — no agent, no tokens |
| `provision` | `prov-*` | Build a repo's warm golden |
| `plan` | `plan-*` | Compare the repo against its goal; file the next issues or declare it met |

Two budgets, deliberately separate:

- **Attempts** (`FACTORY_MAX_ATTEMPTS`, default 3): a crashed build retries on a fresh VM,
  resuming the same branch with the prior log fed back in.
- **Review cycles** (`FACTORY_MAX_REVIEW_CYCLES`, default 2): a rejecting review or red CI
  dispatches a fix run against the same PR.

Issues opt into review by carrying an `## Acceptance criteria` YAML block; `control` parses
it deterministically and the reviewer checks each criterion. On a yes verdict with
`FACTORY_AUTO_MERGE` on, the factory waits for the PR's checks, squash-merges pinned to the
tested sha, and repairs a merge conflict once by merging base into the branch.

State mirrors to the issue as five labels — `agent:queued|running|blocked|done|failed` — the
whole human-facing contract. An open `agent:failed` or `agent:blocked` issue halts that
repo's queue (`FACTORY_HALT_ON_FAILURE`), so the factory never builds on top of a failure.

**The goal loop** (`FACTORY_PLAN`, off by default) is what makes the pipeline self-serving.
A watched repo can carry a *goal* — prose describing the project's endstate, set through the
API and stored on the register. When the queue runs dry for a repo whose goal is `active`,
the poller dispatches a `plan` run: an agent on a VM that reads the repo as it is, judges the
gap against the goal, and either files the next increment of issues (factory-compose format,
labelled `agent:queued`, at most `FACTORY_PLAN_MAX_ISSUES` per pass) or declares the goal
met. The control plane verifies rather than trusts: `met` requires the verdict *and* an
empty queue on GitHub, a queued issue outranks any claim, and `FACTORY_PLAN_MAX_STALLS`
consecutive fruitless plans park the goal as `stalled` for a human. A cooldown
(`FACTORY_PLAN_COOLDOWN`) bounds the cadence, and the hook sits below the halt check — a
halted repo never plans. `control/plan.py` holds the whole loop.

## §5 What it deliberately does not have

Named so they stay unbuilt until something proves they are needed:

- No GitHub webhooks. Polling every 30s is simpler and sufficient at
  `FACTORY_MAX_CONCURRENT=3` runs.
- No queue broker. SQLite plus one active-run check per repo (`db.has_active_run`); a repo
  runs one issue at a time, lowest number first.
- No `ExecutionBackend` abstraction. `control` talks to boxd concretely. The interface gets
  extracted when a second backend exists, not before.
- No OTLP ingest. Telemetry is fed in process (§3.2); the wire transport comes with the
  first out-of-process producer.
- No orchestration inside the VM. One agent per VM, nested agents disabled; the
  build-to-review handoff belongs to the control plane, not the model.

## §6 Related documents

`§` refers to sections within the named file; cross-doc references always name the file.

- `OPERATIONS.md` — running the factory: goldens, deploy, configuration, API surface
- `../control/README.md` — control-plane internals and the boxd SDK rules
- `../telemetry/README.md` — telemetry design: adapters, pricing, tables
- [agent-skills](https://github.com/mithril-studio/agent-skills) — the skills goldens
  install, including the memory skill
