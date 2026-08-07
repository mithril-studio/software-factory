# Software Factory

An agentic development system. GitHub issues go in; reviewed pull requests come out.

A control plane on Hetzner watches repos for work, forks an isolated boxd VM per task, runs
a coding agent inside it, and reaps the VM when done. The control plane is deterministic and
contains no LLM. All intelligence lives in the agent, inside the VM.

> **Status:** V0, started 2026-07-30. Second attempt. The first failed on scope — see
> `../learnings.md`. The governing constraint of this build is *smallness*.

---

## §1 The three layers

One repo, one deployable. Each layer gets a folder so it can be split into its own repo
once it has earned an independent release cycle — build it working first, fragment later.

| Folder | Layer | Responsibility | Contains an LLM? |
|---|---|---|---|
| `control/` | Control plane | Watch issues, fork VMs, dispatch agents, reap, serve the UI | No |
| `telemetry/` | Trace | Ingest OTLP, normalize, store runs and token usage | No |

`control/` rather than `exec/`: `exec` is a Python keyword and cannot name a package.

Skills are **not** a folder here. They live in [agent-skills](https://github.com/mithril-studio/agent-skills) because they
ship into the VM rather than onto the server — they are the agent's code, installed when a
golden is built, and are reused by agents that have nothing to do with this project.

Nothing in this system calls a model except the agent running inside a boxd VM. This is the
**passive-layer principle**: the tools read and write, the caller has the intelligence.

## §2 Topology

```
                 Hetzner VM
   ┌─────────────────────────────────────────┐
   │  exec (FastAPI)          telemetry      │
   │    │  polls GitHub         ▲  OTLP/HTTP │
   │    │                       │            │
   │    └────── Postgres ───────┘            │
   └────┬────────────────────────────────────┘
        │ boxd SDK (gRPC)             ▲
        ▼                             │ OTLP export
   ┌─────────────────────────────┐    │
   │  boxd VM (fork of golden)   │────┘
   │    agent + skills           │
   │    → pushes branch, opens PR│
   └─────────────────────────────┘
```

One fork per task. Forks take ~0.2s, so there is **no warm pool** — provision on demand,
destroy on completion. The agent's isolation boundary is the VM.

## §3 Contracts between layers

These are the only things the layers share. They are written down even inside one repo,
because they are what makes splitting the layers out later cheap. Change them deliberately.

### §3.1 `run.id` — the correlation key

A UUID minted by `exec` when a task is dispatched. It is the join key across every layer.

`control` injects it into the agent VM as an OTel resource attribute:

```
OTEL_RESOURCE_ATTRIBUTES=run.id=<uuid>,issue=<owner/repo#123>,repo=<owner/repo>,vm=<box-name>
```

Every span, metric, and log the agent runtime emits carries these attributes. `telemetry`
uses `run.id` to attach usage to a run without the two layers calling each other.
**They communicate only through the database and this attribute.**

### §3.2 OTLP — the telemetry wire format

`telemetry` accepts OTLP/HTTP. It does not accept a bespoke format, and it is not
Claude-specific: any agent runtime that speaks OpenTelemetry is a valid producer. Runtime
specifics are confined to adapters (`telemetry` §2). This is what keeps the system
model-agnostic.

### §3.3 The memory record schema

Defined by the `memory` skill in [agent-skills](https://github.com/mithril-studio/agent-skills) §3. The agent appends records to `.mem/domains/*.jsonl`
**in the target repo**, and they ship inside the pull request. No service reads or writes
memory — it travels with the code, and it is reviewed like code.

## §4 What V0 deliberately does not have

Named so they stay unbuilt until something proves they are needed:

- No test agent or review agent. One agent, a few skills. Those come after V0 works.
- No warm VM pool. Forks are ~0.2s.
- No `mem` binary. Memory is a skill writing JSONL; the binary is extracted later, once the
  format is proven against real records. See `../memory-engine-spec.md`.
- No `ExecutionBackend` abstraction. `exec` talks to boxd concretely. The interface gets
  extracted when a second backend exists, not before.
- No GitHub webhooks. Polling at 5 concurrent projects is simpler and sufficient.
- No queue broker. Postgres `SELECT ... FOR UPDATE SKIP LOCKED` until it hurts.

## §5 Build order

Revised from layer-by-layer to a vertical slice: build the thinnest path from issue to PR
first, then thicken it. Layer-by-layer means the system only works at the very end, which is
the big-bang shape that killed the first attempt.

1. **`memory` skill** — a markdown file, now in its own repo. Complete.
2. **`control`** — fork, dispatch, collect, reap, plus a UI to watch it. **Done.**
3. **Golden VM + first real run** — the highest-risk unknown: does an agent in a fork
   actually take an issue and open a PR?
4. **`telemetry`** — once runs exist to observe.

## §6 Related documents

- `../learnings.md` — why the first attempt failed and what to carry forward
- `../execution-backends.md` — the isolation ladder and the deferred backend interface
- `../memory-engine-spec.md` — the full `mem` spec, deferred to a future standalone repo
- `../firecracker-vm-pool.md` — the deferred sovereignty layer
