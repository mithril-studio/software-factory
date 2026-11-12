# Software Factory

GitHub issues go in; pull requests into `test` come out.

A control plane watches repos for issues labelled `agent:queued`, restores an isolated boxd VM
per issue, runs a coding agent inside it, and destroys the VM when done. The control plane is
deterministic and contains no LLM. All intelligence lives in the agent, inside the VM.

> **Status:** v1, 2026-11-12. A deliberate step back. The previous version grew an AI review,
> CI gating, auto-merge, scheduled releases, test-server deploys, browser acceptance, a planner
> and a learning loop — about 13,000 lines — faster than it was used. v1 keeps the one
> primitive everything else was built on and makes it reliable. The rest is in git history.

---

## §1 The primitive

```
issue (agent:queued) ──► VM from a golden ──► agent ──► branch + PR into test ──► VM destroyed
```

That is the whole product. A human reviews the pull request and merges it into `test`; with
`FACTORY_SEQUENTIAL` on, the next queued issue starts once that PR is merged or closed, so
issues that depend on each other build on each other. `test` → `main` is a human decision.

What makes it usable rather than merely working:

- **The issue is the interface.** Labels say where it is (`queued`, `running`, `done`,
  `failed`); one comment says where the PR goes and links the live log; one comment at the end
  names the PR, the cost, and whether the run passed the budget.
- **Iterate by re-queuing.** Re-adding `agent:queued` continues the branch. If its PR is open,
  the agent reads the PR's comments first — review feedback is a comment and a label.
- **Nothing is lost on a restart.** Interrupted runs are re-queued on boot and continue from
  what was pushed.
- **Cost is visible per run.** A soft budget (`FACTORY_RUN_BUDGET_EUR`, €100) flags a run that
  passes it without stopping it.

## §2 The layers

| Folder | Layer | Responsibility | Contains an LLM? |
|---|---|---|---|
| `control/` | Control plane | Watch issues, provision VMs, dispatch the agent, report, reap | No |
| `telemetry/` | Trace | Normalize agent events; store model calls, tool calls and cost | No |
| `web/` | UI | React SPA, built to `web/dist` and served by `control` | No |
| `scripts/` | Operations | Goldens, deploy, the CI gate list, health monitoring | No |

Skills live in [agent-skills](https://github.com/mithril-studio/agent-skills) because they
ship into the VM, installed when a golden is built.

```
   boxd VM mithril-factory  (systemd)
   ┌─────────────────────────────────────────┐
   │  control (FastAPI) ──► telemetry        │
   │    │  polls GitHub        │             │
   │    └────── SQLite ────────┘             │
   └────┬────────────────────────────────────┘
        │ boxd SDK
        ▼
   ┌─────────────────────────────┐
   │  boxd VM job-<id>           │
   │  (from golden-<repo> or     │
   │   golden-copy)              │
   │    agent + skills           │
   │    → pushes branch, opens PR│
   └─────────────────────────────┘
```

## §3 Contracts

### §3.1 `run.id`

`uuid4().hex`, minted when a run is claimed. The VM is `FACTORY_VM_PREFIX` + its first 8
characters. It is the join key across layers, and the one telemetry variable the VM gets:

```
OTEL_RESOURCE_ATTRIBUTES=run.id=<id>,issue=<owner/repo#123>,repo=<owner/repo>,vm=<box-name>
```

### §3.2 The telemetry adapter

Runtime specifics are confined to `telemetry/normalize.py`. `control` hands the agent's event
stream to a `Recorder` in process; the dependency runs one way (`control` imports `telemetry`,
never the reverse). Cost is a join against `model_prices`, so prices are data, and a call to a
model with no price is counted as `unpriced_calls` rather than silently costing $0.

### §3.3 The golden launch contract

A golden owes the control plane two files: `/usr/local/bin/factory-agent`, which reads
`$FACTORY_PROMPT` and emits its event stream on stdout, and `/etc/factory/agent.json`, the
manifest naming the agent, its transcript glob and its event format. `control/README.md` §2.3.

### §3.4 The issue

The issue body is the agent's whole brief, so its shape is the contract between the person
writing it and the run. `docs/ISSUE_TEMPLATE.md` is the shape: what and why, acceptance
criteria as checkboxes, out of scope, how to verify. The agent is told to make every criterion
true, respect what is out of scope, and run the verification. Nothing in the control plane
parses it — an issue that does not follow it still runs.

## §4 What comes next, and the rule for adding it

Two ideas are queued behind v1 being used for real:

1. **An issue grill loop.** Before an issue is queued, an agent asks the questions that would
   otherwise blindside the build: it answers what the code can answer, posts the rest as a
   comment, a human answers, and a judge checks the issue against §3.4 before splitting it into
   task issues. The human answering is the point: an agent that answers its own questions
   writes guesses into the brief.
2. **Harnesses after a build.** A label on the issue (`frontend`, `needs-e2e`) starts a second
   run on the same branch with a design, end-to-end or browser harness as its prompt.

The rule: **each of these is a prompt and a skill on the same primitive, chained by labels on
issues — never a new state machine in the control plane.** The previous version broke that
rule once per feature, and that is how it became 13,000 lines nobody used.

## §5 What it deliberately does not have

- **No merge.** A human merges every pull request.
- **No retries.** A failed run is labelled; re-queuing is a human (or a restart) deciding.
- **No model calls in `control/`.**
- **No halting.** A failed issue does not stop the queue behind it.

## §6 Related documents

- [OPERATIONS.md](OPERATIONS.md) — deploying, goldens, connecting repos, configuration
- [ISSUE_TEMPLATE.md](ISSUE_TEMPLATE.md) — the shape of an issue the factory builds well
- [../control/README.md](../control/README.md) — control plane design notes
- [../telemetry/README.md](../telemetry/README.md) — trace layer design notes
