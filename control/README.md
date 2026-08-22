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

- **One base golden, plus one per connected repo.** `golden-copy` carries tooling, auth and
  skills from [agent-skills](https://github.com/mithril-studio/agent-skills) and no repo.
  Restored per task, never worked in directly. The run brings its own repo: it clones the one
  it was assigned into `$HOME/work/<name>` unless a checkout of that repo is already there,
  which is the whole difference between a warm `golden-<repo-slug>` and the base.
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
The `agents` table carries the cheapest one there is: `verified_at`, the last time a run
finished on that snapshot having produced usage. No probe boots a VM to ask — a run using the
credential is the only test of it that proves anything, and the runs happen anyway.

Never `boxd machine share` a golden: sharing deletes the in-VM agent credentials.

### §2.3 What a golden owes the control plane

Two files, and nothing else. Adding a second coding agent is then building a snapshot, not
editing this layer.

- **`/usr/local/bin/factory-agent`** — executable, on `PATH`. The dispatch script `exec`s it
  as its last act, with no arguments: it reads `$FACTORY_PROMPT` and the rest of the
  environment itself, and it must emit its agent's event stream on stdout. The launch line
  belongs to the golden because that is the only place that knows what is installed.
- **`/etc/factory/agent.json`** — the manifest, announced on the line before the handoff as
  `FACTORY-MANIFEST {…}` with its newlines stripped. Known keys:

      {"agent": "claude", "transcript": "\"$HOME\"/.claude/projects/*/*.jsonl",
       "events": "claude-code"}

  `agent` is written onto the run row, so a run records what actually ran and not only what
  was asked for. `transcript` is the glob the salvage step reaches for before the VM is
  reaped. `events` names the telemetry adapter that reads this agent's stream
  (`telemetry/normalize.py`); a name no adapter answers to records no rows rather than
  refusing the run. Every key has a default and the whole file may be missing or broken: an
  unparseable manifest is `{}`, and a run whose real work succeeded never fails over one.

The prompt is multi-kilobyte and quotes arbitrarily, so nothing re-quotes it through another
shell. An executable on `PATH` is what keeps that class of bug impossible; parsing the
manifest in the dispatch script and building a command out of it would reintroduce it.

A golden captured before the wrapper existed still runs: the dispatch scripts fall back to
the `claude -p` line the control plane used to carry. That fallback is the rollback for this
migration and goes away once every golden carries `factory-agent`.

## §3 Dispatch environment

What `exec` injects into every agent run, built by `runner.dispatch_env` for both the build
and the review path — one function, because a review VM that clones a different repo or
authenticates as somebody else than the build VM whose work it checks is not reviewing that
work. The telemetry half is specified in `../telemetry/README.md` §6 and the correlation key
in `../README.md` §3.1.

- `FACTORY_REPO` — the repo this run is for. The golden does not know; the run does.
- `FACTORY_WORKDIR` — where checkouts live. Empty means `$HOME/work`.
- `FACTORY_REPO_DIR` — a pre-clone checkout to reuse, honoured only when it holds
  `FACTORY_REPO`. The rollback for repo-agnostic goldens; it goes away with them.
- `FACTORY_BRANCH`, `FACTORY_BASE`, `FACTORY_RESUME` (build runs only), `FACTORY_PROMPT`
- `GH_TOKEN` — the control plane's durable credential, covering the clone, the push and
  `gh pr create` from one source. Left unset rather than empty when unconfigured, so the
  golden's own `gh` login stays as the fallback instead of being shadowed.
- `ANTHROPIC_API_KEY` / `CLAUDE_CODE_OAUTH_TOKEN` — durable agent auth, overriding the
  golden's expiring OAuth login
- `run.id` and issue/repo/vm identity, as OTel resource attributes
- OTLP endpoint, protocol, and ingest token
- `OTEL_LOGS_EXPORT_INTERVAL=1000` — so an ephemeral VM flushes before it is destroyed

Then drain briefly after the agent exits and before `box.destroy()`, or the tail of every
run is lost.

## §4 Reconciliation

The reason this layer exists rather than handing an agent CLI access: **fleet state lives in
a table, not in a context window.**

A periodic reconciler (`runner.start_reconciler`, every `FACTORY_RECONCILE_INTERVAL` seconds)
compares the boxd fleet against the `runs` table and resolves the difference: a VM with no
active run is an orphan and gets destroyed; a run marked running whose VM is gone is a failure
and gets marked as one. Without this, crashed dispatches leak machines against a 20-machine
quota, silently.

It is also the fallback, not the first line. Every path that creates a machine reaps it in a
`finally`, and `runner.headroom` refuses to provision into a full fleet — sweeping first, since
an orphan is exactly the thing to reclaim before giving up. A single machine that will not die
is reported and left to the next sweep rather than aborting the one it is in.

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
- [x] One golden per project, or per project-and-branch? — per project, named in
      `FACTORY_REPOS`. Branch never enters it: a run checks out its own branch from the base.
- [ ] Which GitHub identity opens the PR, and how the token reaches the fork.
