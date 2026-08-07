# telemetry

The trace layer. Ingests OpenTelemetry from agent runs, normalizes it to a vendor-neutral
schema, and stores it in Postgres.

**You own this data.** Nothing is sent to a model provider or a SaaS backend. The point of
this layer is to see what the agent is doing, in your own database, regardless of which
model or agent runtime produced the work.

> Contract with the rest of the system: `../README.md` §3.

---

## §1 Why not just use an LLM observability SaaS

Because the schema would be someone else's, and the runtime-specific one at that. An agent
runtime like Claude Code emits events named `claude_code.*`. Build tables around those names
and swapping the runtime means rewriting the consumer.

OTLP is the escape hatch: it is a vendor-neutral standard that every major agent runtime
already speaks. So this layer accepts OTLP, and confines all runtime-specific knowledge to
adapters (§2). Adding a runtime is one adapter, not a migration.

## §2 Architecture

```
agent VM ──OTLP/HTTP──► ingest ──► adapter.normalize() ──► Postgres
```

**Ingest** accepts OTLP/HTTP (protobuf and JSON) at `/v1/logs`, `/v1/metrics`, `/v1/traces`.
It validates, authenticates, and does nothing clever.

**Adapters** are the model-agnostic boundary. One per agent runtime. Same shape as the
`ExecutionBackend` pattern in `../../execution-backends.md`:

```python
class TraceAdapter(Protocol):
    name: str
    def matches(self, resource: Resource) -> bool: ...
    def normalize(self, records: list[Record]) -> list[LlmCall | ToolCall | RunEvent]: ...
```

`ClaudeCodeAdapter` ships first. It maps `claude_code.api_request` to an `llm_call` row and
`claude_code.tool_result` to a `tool_call` row. **No table in this repo mentions Claude.**

**Storage** is Postgres. At five concurrent projects this is not close to a scale problem;
revisit only when event volume reaches millions of rows. Partition by day before reaching
for ClickHouse.

## §3 Tables

| Table | Owner | Contents |
|---|---|---|
| `runs` | `exec` | Run lifecycle. This layer reads it, never writes it. |
| `llm_calls` | `telemetry` | `run_id`, provider, model, input/output tokens, duration, ts |
| `tool_calls` | `telemetry` | `run_id`, tool name, success, duration, error type, ts |
| `model_prices` | `telemetry` | Price per million tokens, per provider/model, with valid-from |

`exec` and `telemetry` never call each other. They share the database and the `run.id`
resource attribute, and that is the entire coupling.

## §4 Tokens are the unit, not dollars

Store `input_tokens`, `output_tokens`, provider, and model. **Do not store the emitter's
reported cost.**

On a MAX subscription the marginal cost of a run is zero, so any `cost_usd` a runtime
reports is an imputed number against list price — accurate as a usage signal, misleading as
a financial one. Cost is therefore a *derived* value: join `llm_calls` against
`model_prices`, which is a table you control.

This is agnostic by construction, and it starts producing true numbers the moment a run
points at a metered model.

## §5 Two things the ephemeral VMs break

Both are consequences of fork → run → destroy, and both silently lose data if ignored.

**§5.1 Flush timing.** OTel's default log export interval is 5s, metrics 60s. If the VM is
destroyed the instant the agent exits, the tail of every run is lost — traces that stop just
before the interesting part. Mitigation is on the `exec` side: set
`OTEL_LOGS_EXPORT_INTERVAL=1000` and drain briefly before `box.destroy()`.

**§5.2 The transcript is the backstop.** The live stream will have holes. The agent runtime
writes a complete session JSONL to the VM's disk; `exec` salvages it with `box.read_file()`
before reaping, and stores it as the durable artifact for the run. The stream is for
watching; the transcript is for replaying.

## §6 Producer configuration

`exec` sets these when dispatching an agent. Recorded here because this layer defines the
contract; see `../README.md` §3.1.

```bash
CLAUDE_CODE_ENABLE_TELEMETRY=1
OTEL_LOGS_EXPORTER=otlp
OTEL_METRICS_EXPORTER=otlp
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
OTEL_EXPORTER_OTLP_ENDPOINT=https://<hetzner-host>/otel
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer <ingest-token>
OTEL_LOGS_EXPORT_INTERVAL=1000
OTEL_RESOURCE_ATTRIBUTES=run.id=<uuid>,issue=<owner/repo#N>,repo=<owner/repo>,vm=<box-name>
```

The first line is runtime-specific and belongs to the producer, not to this layer. Every
other line is standard OTel.

## §7 V0 scope

Ships:
1. OTLP/HTTP ingest for logs and metrics, bearer-token auth
2. `ClaudeCodeAdapter`
3. `llm_calls`, `tool_calls`, `model_prices` + migrations
4. One read endpoint: usage by `run.id`

Deferred: traces (the runtime's trace export is still beta), a UI, alerting, retention
policy, any second adapter.

## §8 Open

- [ ] Confirm what Claude Code actually emits over OTLP end to end, against a real ingest.
      The docs are the map, not the territory — verify before building the adapter.
- [ ] Ingest auth: a single shared bearer token, or one per run derived from `run.id`?
- [ ] Retention. Transcripts are large; `llm_calls` rows are not.
