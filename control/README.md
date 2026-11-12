# control

The control plane. Runs on a boxd machine (`mithril-factory`) under systemd. Watches GitHub for
issues labelled `agent:queued`, restores an isolated boxd VM per issue, runs an agent in it,
finds the pull request it opened, and destroys the VM.

**This layer is deterministic and contains no LLM.** Every intelligent decision happens inside
the VM. If a model call ever appears in this repo, something has gone wrong architecturally.

> Contract with the rest of the system: `../docs/architecture.md`.

---

## §1 The loop

```
poll GitHub (poller)
  └─► lowest agent:queued issue on a repo with nothing in flight
      └─► claim: runs row (db.claim_run, atomic) + agent:queued removed on GitHub
          └─► ensure target branch (test) exists
              └─► restore golden ─► exec agent (stream log + telemetry)
                  └─► find open PR ─► salvage transcript ─► destroy VM
                      └─► agent:done / agent:failed + a comment with PR and cost
```

Every step is a status transition on one `runs` row: `queued → provisioning → running →
succeeded | failed | cancelled`. Nothing merges; a human merges the PR into `test`.

| Module | Concern |
|---|---|
| `poller` | Discovery: the next `agent:queued` issue per repo, lowest number first; waits for an open factory PR when `FACTORY_SEQUENTIAL` is on |
| `runner` | The run lifecycle: claim, provision, stream, report, reap; plus restart recovery and the reconciler |
| `agents` | Snapshot resolution: `golden-<repo-slug>` → `golden-copy` |
| `preflight` | Is a repo ready? Readable, pushable, labels, golden — boots nothing |
| `github` | Every GitHub API call |
| `repos` | The repo register and each repo's target branch |
| `db` | SQLite: `runs` and `repos`. `init()` is idempotent and only adds columns |
| `auth` | Session login; everything under `/api` except login sits behind it |
| `memory` | The `.mem/` validator — this repo's own CI gate, not part of a run |
| `app` | FastAPI: routes, static `web/dist`, startup wiring |

Three rules the module layout does not show:

1. **Taking `agent:queued` off the issue is the claim, and it is not best-effort.** If it
   fails, the run stops before it spends a VM; otherwise the poller would dispatch the same
   issue again after the run ends. Every other label or comment write is best-effort.
2. **The VM name is written to the row before the VM exists**, so the reconciler can never
   mistake a machine being created for an orphan.
3. **A restart re-queues, it does not lose.** On boot, `runner.recover()` closes every
   unfinished run and puts its issue back on `agent:queued`; the next run continues from what
   was pushed. On a graceful stop, `runner.shutdown()` cancels runs so each one's `finally`
   destroys its VM first.

## §2 boxd

Programmatic access is the **Python SDK**, not the CLI. It is async, so it matches FastAPI
directly, and it auto-refreshes JWTs from the API key.

```python
from boxd import AsyncBoxd

boxd = AsyncBoxd(api_key=...)
machine = await boxd.machines.create(name=name, from_snapshot=golden,
                                     auto_suspend_timeout=0,
                                     auto_destroy_timeout=settings.auto_destroy)
await boxd.machines.wait_until_ready(machine.id, timeout=180)
async with boxd.machines.stream_exec(machine.id, command=..., env=...) as stream:
    async for chunk in stream.iter_chunks():
        ...
await boxd.machines.delete(machine.id)
```

Snapshot restore is the primary path; `machines.fork` survives only as the rollback for
sources that are still machines rather than snapshots. `auto_destroy_timeout` is
load-bearing: without it, a control plane that dies mid-run leaks the machine for good.

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
- **No idle pool.** Restores are ~0.2s, so machines exist only while a run does. Every one
  is named `FACTORY_VM_PREFIX` + the run id (`job-1a2b3c4d`), and that prefix is the only
  thing the reconciler will ever delete — another control plane on the same account, a
  golden kept as a machine, or a test server are invisible to it.
- **`auto-suspend.timeout = 0` on every machine.** The default suspends after 30s without
  inbound TCP, and clocks freeze while suspended. A long build or test run with no network
  traffic looks idle and gets frozen mid-work.
- **Quota is 20 machines** (free plan, 2 vCPU / 8 GB each). Cap concurrency below it and
  reap on a timer, not only on success.

### §2.2 Golden auth

The golden carries an interactive agent login on disk, inherited by every restore. It
expires. Rotation is: re-authenticate the golden, re-snapshot, done — a scheduled chore, not
a one-time setup step. A stale credential surfaces as a mysteriously broken agent, not as an
auth error. A run using the credential is the only test of it that proves anything.

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

A golden without `factory-agent` fails the run with exit 90 before the agent starts.

## §3 Dispatch environment

What `runner.dispatch_env` injects into every run. Values travel as environment variables and
are never interpolated into the script, so issue text cannot become shell.

- `FACTORY_REPO` — the repo this run is for. The golden does not know; the run does.
- `FACTORY_WORKDIR` — where checkouts live. Empty means `$HOME/work`.
- `FACTORY_BRANCH`, `FACTORY_BASE` (the target branch), `FACTORY_PROMPT`
- `FACTORY_RESUME` — `1` continues the remote branch (its PR is open, or it never had one);
  `0` means its PR was closed or merged, so the stale branch is deleted and the run starts
  fresh from the target. Decided here, because only the control plane sees the PR history.
- `FACTORY_AGENT_EFFORT` — reasoning effort, passed through to the agent
- `BASH_DEFAULT_TIMEOUT_MS` / `BASH_MAX_TIMEOUT_MS` — command timeouts inside the agent
- `GH_TOKEN` — covers the clone, the push and `gh pr create`. Left unset rather than empty
  when unconfigured, so the golden's own `gh` login stays as the fallback.
- `ANTHROPIC_API_KEY` / `CLAUDE_CODE_OAUTH_TOKEN` — durable agent auth
- `OTEL_RESOURCE_ATTRIBUTES` — run/issue/repo/vm identity

## §4 Reconciliation

**Fleet state lives in a table, not in a context window.** Every `FACTORY_RECONCILE_INTERVAL`
seconds `runner.reconcile` compares the boxd fleet with the `runs` table: a machine carrying
our prefix that no unfinished run claims is destroyed, and an unfinished run with no task
behind it (older than two minutes) is failed. It is the fallback: every run reaps its own VM
in a `finally`, and `_headroom` sweeps before refusing to provision into a full fleet.

## §5 Cost

The run's cost is what the runtime reports in its final event (`runs.cost_usd`), backfilled
from telemetry rows when a run dies before reporting. Each closed turn, the derived cost is
compared with `FACTORY_RUN_BUDGET_EUR`; past it the run is flagged (`runs.over_budget`), logged
and reported on the issue, and keeps running.

## §6 Where things are

Routes live in `app.py`. Configuration and defaults live in `config.py`, annotated in
`../.env.example`. Operating the thing — goldens, deploy, config reference — is
`../docs/OPERATIONS.md`.
