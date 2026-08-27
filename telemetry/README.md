# telemetry

The trace layer. Normalizes agent-run events to a vendor-neutral schema and stores them one
row per model call and one row per tool call, so cost, time and outcome can be joined
without parsing a transcript after the fact.

**You own this data.** Nothing is sent to a model provider or a SaaS backend. The point of
this layer is to see what the agent is doing, in your own database, regardless of which
model or agent runtime produced the work.

> Contract with the rest of the system: `../docs/architecture.md` §3.

---

## §1 Why not just use an LLM observability SaaS

Because the schema would be someone else's, and the runtime-specific one at that. An agent
runtime like Claude Code emits events named `claude_code.*`. Build tables around those names
and swapping the runtime means rewriting the consumer. So all runtime-specific knowledge is
confined to one adapter (§2), and adding a runtime is one more of those, not a migration.

The other reason is that an APM product asks the wrong question. Its job is *is it up, is it
fast, is it erroring* — and an agent run can be fast, error-free, exit 0 and open a pull
request that fails review. That run is a total loss and every one of those checks is green.
The question worth instrumenting is **did the work land, and what did it cost**, which is
why every row here joins to `runs` and why §7's read endpoints report cost per shipped issue
and spend on runs that shipped nothing.

## §2 Architecture

```
                              ┌── recorder.py ──┐
agent VM ──stream──► control.runner._stream ────┼──► adapter.feed() ──► SQLite
                                                │         ▲
transcript on disk ──► backfill.py ─────────────┘         │
                                                          │
             (later) agent VM ──OTLP/HTTP──► ingest ──────┘
```

**Producers.** Two today, both feeding the same normalizer. `recorder.py` rides the event
stream the control plane already parses, in process, with the `run_id` in hand.
`backfill.py` replays a salvaged transcript after the fact. An OTLP ingest becomes a third
producer, not a second implementation.

> **Why not OTLP first, as this spec originally said.** The facts telemetry wants — every
> model call and every tool call — already stream through `control.runner._stream()`, structured,
> with the correlation key attached. Routing them out of the VM over OTLP to get them back
> would add an HTTP server, protobuf, ingest auth, a second network path out of an
> ephemeral machine, and the flush-timing data loss in §5.1 — to deliver the same rows. So
> the adapter boundary was built first and the transport deferred. OTLP remains the right
> long-term boundary and the only way to see inside a subagent; it is a later addition to
> a working layer rather than the precondition for one.

**Adapters** are the model-agnostic boundary — the one place that knows what a runtime's
events look like:

```python
class ClaudeCodeAdapter:            # telemetry/normalize.py
    name = "claude-code"
    def feed(self, event: dict) -> list[LlmCall | ToolCall]: ...
    def flush(self) -> list[ToolCall]: ...   # tools the run died inside
    def summary(self, event: dict) -> dict:  # the run's own figures, {} for most events

ADAPTERS = {"claude-code": ClaudeCodeAdapter}
adapter_for(events, run_id)         # anything unknown -> NullAdapter
```

Pure functions, no I/O, no clock — which is what lets one implementation serve the live
stream and a replayed transcript, and what makes `normalize_test.py` possible.
**No table in this repo mentions Claude.**

Which adapter a run uses comes from the `events` string in the golden's manifest
(`control/README.md` §2.3), handed to `Recorder(run_id, events)` or to `recorder.use()`
when the manifest arrives — before the first event, since an adapter is stateful over the
stream it reads. A missing manifest or an absent `events` key defaults to `claude-code`;
`backfill.py` does not consult the manifest at all — it replays every transcript through
`ClaudeCodeAdapter`, and a replay is authoritative (`clear_run` first, then rewrite).

`summary()` lives on the adapter for the same reason `feed()` does: Claude Code reports a
run's own token and cost figures on a final event it calls `result`, and that sentence is
one runtime's vocabulary. The control plane asks every event and keeps whatever comes
back, so it never has to know the word.

**An agent with no adapter still runs.** A non-empty `events` string this layer does not
recognise selects `NullAdapter`, whose every answer is empty: no rows, no figures, no exception.
Telemetry is a consumer of a run and never a precondition for one, so the first run of a
new agent records nothing and works — and reports no cost rather than a wrong one, with
`runner._salvage_usage` filling the ledger from rows wherever there are any. Writing the
adapter comes after, against events recorded from that real run rather than invented.

**Storage** is `control`'s own SQLite file, same reasoning as `control/db.py`: plain SQL,
so Postgres is a driver swap rather than a rewrite. Revisit when event volume reaches
millions of rows; partition by day before reaching for ClickHouse.

**Dependency direction is one-way.** `control` imports `telemetry`; `telemetry` never
imports `control` — it has its own `config.py` naming the shared paths. The layers still
communicate only through the database and `run.id` (§3.1 of `docs/architecture.md`); the
import is the producer knowing its sink, and it disappears when OTLP arrives.

## §3 Tables

| Table | Owner | Contents |
|---|---|---|
| `runs` | `control` | Run lifecycle. This layer reads it, never writes it. |
| `llm_calls` | `telemetry` | `run_id`, `turn`, model, in/out/cache-read/cache-write tokens, ts, `parent_call_id` |
| `tool_calls` | `telemetry` | `run_id`, `turn`, tool, ok, `duration_ms`, error, detail, ts, `parent_call_id` |
| `model_prices` | `telemetry` | Per-million rates for every token class, per model, with `valid_from` |
| `memory_reads` | `telemetry` | One row per memory record a run opened while priming (§3.3) |
| `memory_receipts` | `telemetry` | One row per run: what its `FACTORY_MEMORY` receipt reported (§3.3) |

The coupling with `control` is the one-way import described in §2 plus the shared database
and `run.id`.

### §3.1 The correlation hierarchy

`run.id` is the middle of a chain, not the whole of it. What makes a failure legible is
knowing where in that chain it happened:

```
repo + issue_number   the unit of work            (runs)
  └─ attempt          retry number                (runs)
      └─ run.id       one VM, kind=build|review   (runs)
          └─ turn     one model call              (llm_calls, tool_calls)
              └─ call one tool use                (tool_calls)
                  └─ parent_call_id  subagent linkage
```

Without it a failure reads *"run 6f2eb679 failed, exit 1, $14"*. With it: *"issue #23,
attempt 2, turn 41, Bash, no result — the run died inside a command."*

**`parent_call_id` is empty today, by design.** Nested agents are disabled in the runner
(`--disallowed-tools Agent Task ScheduleWakeup`), so nothing populates it. The column exists now because
the field is native to the event envelope (`parent_tool_use_id`), so re-enabling subagents
needs no migration — but whether their events reach the parent stream at all is unverified,
and closing that blind spot properly is what OTLP is for.

### §3.2 Cache tokens are split by TTL

`cache_write_5m_tokens` and `cache_write_1h_tokens` are separate columns because they are
priced differently — 1.25x input against 2x. The agent runtime requests 1-hour caching, so
collapsing them would understate the second-largest component of a run's bill by ~40%. A
payload that reports only a combined figure is attributed to the cheaper tier: an unknown
split may never inflate a derived cost.

### §3.3 Memory telemetry

Whether memory retrieval is happening is a claim worth checking against rows, not receipts
taken on faith — so the control plane parses the run's `FACTORY_MEMORY` line
(`../docs/architecture.md` §3.3) and writes it through this layer: one `memory_reads` row per
record the run opened, one `memory_receipts` row per run saying what it indexed and which
domains it drew from. `usage_for_run` returns both under `memory` / `memory_receipt`, and
`memory_metrics_by_repo` is the fifth key of `GET /api/telemetry`. `clear_run` clears these
tables too, so a backfill stays idempotent.

Two properties of the producer worth knowing: the `Recorder` never raises — every failure
increments a `dropped` counter surfaced in the run log — and `Recorder.totals()` is what
`runner._salvage_usage` reads to fill a dead run's ledger entry from the rows already
written.

## §4 Tokens are the unit, not dollars

Store tokens, model, and timestamp. Cost is a **derived** value: join `llm_calls` against
`model_prices`, a table you control, matching on the price whose `valid_from` was in effect
when the call happened. Re-pricing the future never rewrites the past — you add a row, you
never edit one.

On a subscription the marginal cost of a run is zero, so any `cost_usd` a runtime reports is
an imputed number against list price — accurate as a usage signal, misleading as a financial
one. This is agnostic by construction, and it starts producing true numbers the moment a run
points at a metered model.

**The emitter's `cost_usd` is nonetheless kept on `runs`,** which is a deliberate departure
from this section's original "do not store it". It costs one column and buys a cross-check:
reported and derived should agree to within the runtime's own side calls (title generation
and the like never surface as assistant events), and the two diverging by more than that is
a signal that the price table or the normalizer is wrong. It is also the fallback when a run
never reports at all — see §5.3.

## §5 Three things the ephemeral VMs break

All are consequences of fork → run → destroy, and all silently lose data if ignored.

**§5.1 Flush timing.** OTel's default log export interval is 5s, metrics 60s. If the VM is
destroyed the instant the agent exits, the tail of every run is lost — traces that stop just
before the interesting part. The mitigation — a short export interval and a drain before the
VM is deleted — is deferred with the ingest: the in-process recorder has no export interval
to lose the tail to. The runner still drains the stream briefly before reaping.

**§5.2 The transcript is the backstop.** The live stream will have holes. The agent runtime
writes a complete session JSONL to the VM's disk; `control` salvages it before reaping, and
stores it as the durable artifact for the run. The stream is for watching; the transcript is
for replaying — `backfill.py` reads exactly that file through the same normalizer.

**§5.3 Write as you go, or you only measure success.** A run reports its own usage once, in
a final `result` event. A timeout, a crash or a reaped VM means that event never arrives —
so before this layer existed, failed runs recorded `cost_usd = NULL`: about $13 of real
spend absent from our own ledger, on precisely the runs worth understanding.

So rows are flushed **per turn**, not per run. The most a catastrophic failure now loses is
the turn in flight, and `runner._salvage_usage()` reads the rows back to fill in the run's
ledger entry when the runtime never got to. It only ever fills a gap — a run that reported
its own numbers keeps them.

This is the same discipline as an agent committing often instead of once at the end. That
one saves the agent's work; this one saves the record of what happened.

## §6 Producer configuration

The producer contract is one variable, set by `runner.dispatch_env`:

```bash
OTEL_RESOURCE_ATTRIBUTES=run.id=<id>,issue=<owner/repo#N>,repo=<owner/repo>,vm=<box-name>
```

Review runs append `,kind=review`. See `../docs/architecture.md` §3.1. The OTLP exporter
block (endpoint, protocol, ingest token, export interval) arrives with the ingest and not
before — nothing sets it today.

## §7 V0 scope — shipped

| | File |
|---|---|
| `ClaudeCodeAdapter` — pure, testable, both producers | `normalize.py` |
| All six tables (§3) + seed prices, and every read query | `store.py` |
| Live recorder, flushed per turn | `recorder.py` |
| Transcript replay, idempotent | `backfill.py` |
| Cases against real captured events, the store, the recorder | `normalize_test.py`, `store_test.py`, `recorder_test.py` |
| `GET /api/runs/{id}/telemetry`, `GET /api/telemetry` | `control/app.py` |
| Telemetry page and per-run trace panel | `web/src/pages/` |

Deferred and named so they stay unbuilt: OTLP ingest (§2), traces, alerting, retention,
any second adapter, subagent visibility (§3.1).

## §8 Open

- [x] ~~Confirm what Claude Code actually emits.~~ Verified 2026-08-17 against a live
      `--output-format stream-json` run and against **37 production transcripts** replayed
      from the control-plane VM. Field names in `normalize_test.py` are captured, not
      assumed.
- [x] ~~Validate against the full spread of real runs.~~ Done, and it found a bug the small
      local run could not: the transcript writes **one entry per content block, repeating
      the same `usage` object on each** (up to 13 entries for one message), so counting per
      entry inflated tokens ~2x. Fixed by deduplicating on `message.id`. After the fix,
      derived output tokens match the runtime's own reported figure **exactly** on every
      run, and derived cost matches reported **to the cent on 15 of 21** runs (median
      -0.0%, worst -7.7%, the remainder being transcript gaps per §5.2).
      Correcting `model_prices` was the other half: these runs were Sonnet 5 billed at the
      standard $3/$15, not the published introductory $2/$10 — which is precisely the
      cross-check §4 keeps `cost_usd` for.
- [ ] **Run `.venv/bin/python -m telemetry.backfill` on the control-plane VM** to replay its
      salvaged transcripts into rows. (The tables themselves exist — `store.init()` runs on
      every boot.) A single run replays with `... -m telemetry.backfill <run_id>`.
- [ ] Reconcile resumed sessions. One run (`937b51a7`) reports $24.40 against $1.86 derived
      while its token counts match exactly — the runtime's `total_cost_usd` looks cumulative
      across a resumed session, where the rows are per-run. 10 of 47 runs have no salvaged
      transcript at all.
- [ ] Retention. Transcripts are large; `llm_calls` rows are not.
- [ ] Ingest auth, when OTLP lands: one shared bearer token, or one per run derived from
      `run.id`?
- [ ] Keep `model_prices` current. It is a hand-maintained table by design; a stale row
      makes every derived number quietly wrong.
