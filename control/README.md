# control

The control plane. Runs on the long-lived boxd VM `software-factory`. Watches GitHub for
work, forks an isolated boxd VM per task, runs an agent inside it, and reaps it.

**This layer is deterministic and contains no LLM.** It is a state machine over VMs. Every
intelligent decision happens inside the VM. If a model call ever appears in this repo,
something has gone wrong architecturally.

> Contract with the rest of the system: `../README.md` §3.

---

## §1 The loop

```
poll GitHub issues
  └─► claim task            (Postgres, SELECT ... FOR UPDATE SKIP LOCKED)
      └─► fork golden       (~0.2s, boxd SDK)
          └─► exec agent    (telemetry env + run.id injected)
              └─► agent pushes branch, opens PR
                  └─► salvage transcript
                      └─► drain telemetry, destroy VM
```

Every step writes a lifecycle row to `runs`. A run that dies mid-flight is visible as a row
stuck in a state, which is what makes the reconciler possible (§4).

## §2 boxd

Programmatic access is the **Python SDK**, not the CLI. It is async, so it matches FastAPI
directly, and it auto-refreshes JWTs from the API key.

```python
from boxd import AsyncBoxd

boxd = AsyncBoxd(api_key=...)
machine = await boxd.machines.fork(golden, name, auto_suspend_timeout=0)
await boxd.machines.wait_until_ready(machine.id, timeout=180)
async with boxd.machines.stream_exec(machine.id, command=..., env=...) as stream:
    async for chunk in stream.iter_chunks():
        ...
await boxd.machines.delete(machine.id)
```

> The SDK restructured between 0.1.9 and 0.2.2 (`Compute`/`box.*` became
> `Boxd`/`machines.*`, and file-transfer helpers went away — the transcript is now
> salvaged with a plain `exec`). Pin `boxd>=0.2.2`.

Do not shell out to the `boxd` binary. The SDK gives typed errors (`QuotaExceededError`,
`NotFoundError`, `AuthenticationError`), connection reuse, and streaming exec — all of which
would otherwise be stdout parsing against a binary that auto-updates underneath you.

### §2.1 Machines

- **One golden per project.** Dependencies pre-installed, agent authenticated, skills
  installed from [agent-skills](https://github.com/mithril-studio/agent-skills). Forked per task, never worked in directly.
- **No warm pool.** Forks are ~0.2s. Provision on demand.
- **`auto-suspend.timeout = 0` on every fork.** The default suspends after 30s without
  inbound TCP, and clocks freeze while suspended. A long build or test run with no network
  traffic looks idle and gets frozen mid-work.
- **Quota is 20 machines** (free plan, 2 vCPU / 8 GB each). Cap concurrency below it and
  reap on a timer, not only on success.

### §2.2 Golden auth

The golden carries an interactive agent login on disk, inherited by every fork. It expires.
Rotation is: re-authenticate the golden, re-snapshot, done — a scheduled chore, not a
one-time setup step. A stale credential surfaces as a mysteriously broken agent, not as an
auth error, so it is worth a health check that runs before the credential's expected expiry.

Never `boxd machine share` a golden: sharing deletes the in-VM agent credentials.

## §3 Dispatch environment

What `exec` injects into every agent run. The telemetry half is specified in
`../telemetry/README.md` §6 and the correlation key in `../README.md` §3.1.

- `run.id` and issue/repo/vm identity, as OTel resource attributes
- OTLP endpoint, protocol, and ingest token
- `OTEL_LOGS_EXPORT_INTERVAL=1000` — so an ephemeral VM flushes before it is destroyed

Then drain briefly after the agent exits and before `box.destroy()`, or the tail of every
run is lost.

## §4 Reconciliation

The reason this layer exists rather than handing an agent CLI access: **fleet state lives in
a table, not in a context window.**

A periodic reconciler compares `c.box.list()` against the `runs` table and resolves the
difference: a VM with no active run is an orphan and gets destroyed; a run marked running
whose VM is gone is a failure and gets marked as one. Without this, crashed dispatches leak
machines against a 20-machine quota, silently.

Idempotency everywhere. Every operation must be safe to retry.

## §5 V0 scope

Ships:
1. GitHub issue polling (60s; no webhooks at this scale)
2. `POST /runs`, `GET /runs/{id}`, `POST /runs/{id}/cancel`, `GET /machines`
3. Fork → dispatch → salvage → reap
4. The reconciler

Deferred: test and review agents, `ExecutionBackend` abstraction, webhooks, a queue broker,
multi-agent orchestration of any kind.

## §6 Open

- [ ] Prompt assembly. Issue title and body plus repo context is the V0 answer — this is the
      part that quietly grows into a monster, so keep it dumb and let memory carry the
      context instead.
- [ ] Result callback: agent POSTs on completion vs. `exec` awaiting the exec stream.
- [ ] One golden per project, or per project-and-branch?
- [ ] Which GitHub identity opens the PR, and how the token reaches the fork.
