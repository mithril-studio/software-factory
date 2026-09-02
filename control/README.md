# control

The control plane. Runs on a Hetzner VM under systemd. Watches GitHub for work, restores an
isolated boxd VM per task, runs an agent inside it, reviews and merges what it produces, and
reaps the VM.

**This layer is deterministic and contains no LLM.** It is a state machine over VMs. Every
intelligent decision happens inside the VM. If a model call ever appears in this repo,
something has gone wrong architecturally.

> Contract with the rest of the system: `../docs/architecture.md` §3.

---

## §1 The loop

```
poll GitHub issues
  └─► claim issue           (SQLite; db.has_active_run — one run per repo)
      └─► restore golden    (~0.2s snapshot restore, boxd SDK)
          └─► exec agent    (dispatch env + run.id injected)
              └─► agent pushes branch, opens PR
                  └─► salvage transcript, harvest memory candidates, destroy VM
                      └─► review run ─► CI ─► merge   (fix cycles loop back)

queue dry (FACTORY_PLAN)
  └─► sync `.factory/goal.md` by SHA   (a commit arms the goal, or re-arms met/stalled)
      └─► plan run          (agent on a VM: repo vs the goal file, one feature per pass)
          └─► files an `agent:feature` parent + `agent:queued` sub-issues ──► the loop above
              or declares the goal met ──► repo idles until its goal file changes
```

Every phase of an issue is a `runs` row of its own `kind` — `build`, `review`, `ci`,
`provision`, `plan` — and every step is a status transition on that row. A run that dies
mid-flight is visible as a row stuck in a state, which is what makes the reconciler possible
(§4). The pipeline itself — budgets, criteria, labels, merge — is `../docs/architecture.md`
§4.

One module per concern:

| Module | Concern |
|---|---|
| `poller` | Discovery: find the next `agent:queued` issue, lowest number first |
| `runner` | The whole run lifecycle: dispatch, stream, review, merge, reap |
| `agents` | Snapshot resolution: `golden-<repo-slug>` → `golden-copy` |
| `goldens` | Periodic re-list of golden snapshots into the `snapshots` table |
| `provision` | Build a repo's warm golden, as a run of `kind='provision'` |
| `plan` | The goal loop: sync `.factory/goal.md`, plan the next feature toward it when the queue runs dry |
| `preflight` | Is a repo ready? Labels, token scopes, golden — boots nothing |
| `github` | Every GitHub API call: issues, labels, PRs, checks, merge |
| `memory` | The `.mem/` validator; a CI gate (§5) |
| `repos` | The repo register: connected at runtime, seeded from `FACTORY_REPOS` |
| `auth` | Session login; everything under `/api` except login sits behind it |
| `db` | SQLite schema and queries; `db.init()` is idempotent, no migrations |
| `app` | FastAPI: routes, static `web/dist`, startup wiring |

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
- **No idle pool.** Restores are ~0.2s, so machines exist only while a run does.
- **`auto-suspend.timeout = 0` on every machine.** The default suspends after 30s without
  inbound TCP, and clocks freeze while suspended. A long build or test run with no network
  traffic looks idle and gets frozen mid-work.
- **Quota is 20 machines** (free plan, 2 vCPU / 8 GB each). Cap concurrency below it and
  reap on a timer, not only on success.

### §2.2 Golden auth

The golden carries an interactive agent login on disk, inherited by every restore. It
expires. Rotation is: re-authenticate the golden, re-snapshot, done — a scheduled chore, not
a one-time setup step. A stale credential surfaces as a mysteriously broken agent, not as an
auth error, so it is worth a health check that runs before the credential's expected expiry.
The `snapshots` table carries the cheapest one there is: `verified_at`, the last time a run
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

What `control` injects into every agent run, built by `runner.dispatch_env` for both the
build and the review path — one function, because a review VM that clones a different repo or
authenticates as somebody else than the build VM whose work it checks is not reviewing that
work. The correlation key is `../docs/architecture.md` §3.1.

- `FACTORY_REPO` — the repo this run is for. The golden does not know; the run does.
- `FACTORY_WORKDIR` — where checkouts live. Empty means `$HOME/work`.
- `FACTORY_REPO_DIR` — a pre-clone checkout to reuse, honoured only when it holds
  `FACTORY_REPO`. The rollback for repo-agnostic goldens; it goes away with them.
- `FACTORY_BRANCH`, `FACTORY_BASE`, `FACTORY_RESUME` (build runs only), `FACTORY_PROMPT`
- `FACTORY_MEMORY_CANDIDATES` (build runs only) — the JSONL path the run may append memory
  candidates to; the runner harvests it before reaping (§5)
- `FACTORY_AGENT_EFFORT` — reasoning effort, passed through to the agent
- `BASH_DEFAULT_TIMEOUT_MS` / `BASH_MAX_TIMEOUT_MS` — command timeouts inside the agent
- `GH_TOKEN` — the control plane's durable credential, covering the clone, the push and
  `gh pr create` from one source. Left unset rather than empty when unconfigured, so the
  golden's own `gh` login stays as the fallback instead of being shadowed.
- `ANTHROPIC_API_KEY` / `CLAUDE_CODE_OAUTH_TOKEN` — durable agent auth, overriding the
  golden's expiring OAuth login
- `OTEL_RESOURCE_ATTRIBUTES` — run/issue/repo/vm identity, plus `kind=review` on review
  runs. The only telemetry variable there is; the collection itself happens in process
  (`../telemetry/README.md` §2).

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

## §5 Memory machinery

The record schema and the write rules belong to the memory skill (in
[agent-skills](https://github.com/mithril-studio/agent-skills)); this layer's part is
deterministic plumbing:

- **The receipt.** The build prompt requires one `FACTORY_MEMORY {json}` line right after
  priming. `runner.parse_memory_receipt` reads it off the stream and persists it through
  `telemetry` (`memory_reads`, `memory_receipts`), so whether memory is being read is a
  queryable fact rather than a hope.
- **The candidate queue.** `runner._collect_memory_candidates` pulls
  `$FACTORY_MEMORY_CANDIDATES` out of the VM before reaping — bounded (64 kB, 20 records),
  scope-checked to repo-relative paths, content-addressed — into the `memory_candidates`
  table. Triage is `POST /api/memory/candidates/{id}/accept|reject`, exactly once per
  candidate. Accepting records a verdict; nothing here writes `.mem/`.
- **The validator.** `python -m control.memory validate [repo-path]` checks a `.mem/` store's
  shape and is a CI gate via `scripts/verify.sh`. Pure: reads, reports, changes nothing.

## §6 Where things are

Routes live in `app.py`, all under `/api` and behind the session auth gate except `/api/login`
and `/healthz`. Configuration and defaults live in `config.py`, annotated in `../.env.example`.
Operating the thing — goldens, deploy, config reference — is `../docs/OPERATIONS.md`.
