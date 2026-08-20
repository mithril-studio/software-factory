import { useEffect, useRef } from "react"
import { Link, useParams } from "react-router-dom"
import { ArrowLeft } from "lucide-react"
import { post, usePoll, useRunLog, TERMINAL, runOutcome, type Run, type RunTelemetry } from "@/lib/api"
import { cost, duration, shortId, tokens } from "@/lib/format"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { StateBadge } from "@/components/StateBadge"
import { Meter } from "@/components/Meter"
import { PageHeader, SectionHead, ErrorNote } from "@/components/Page"
import { cn } from "@/lib/utils"

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="border-l border-subtle pl-3">
      <div className="eyebrow text-muted-foreground">{label}</div>
      <div className="mt-1.5 font-mono text-sm">{children}</div>
    </div>
  )
}

/** Fixed hues, not the theme tokens. The log panel keeps its ink ground in both
 *  themes, so the light-mode `ok`/`warn`/`bad` values — darkened to sit on paper —
 *  would be unreadable here. These are the same four hues, lifted for a dark ground. */
function lineClass(line: string): string {
  if (line.startsWith("[factory]")) return "text-[hsl(16_75%_66%)]"
  if (line.startsWith("[tool]")) return "text-[hsl(38_90%_62%)]"
  if (line.startsWith("[agent]")) return "text-[hsl(152_45%_58%)]"
  if (line.startsWith("[stderr]")) return "text-[hsl(4_78%_66%)]"
  return "text-white/75"
}

export function RunDetail() {
  const { runId = "" } = useParams()
  const { data: run, error, refresh } = usePoll<Run>(`/api/runs/${runId}`, 3000)
  const { data: trace } = usePoll<RunTelemetry>(`/api/runs/${runId}/telemetry`, 10000)
  const { lines, done } = useRunLog(runId)
  const logRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const el = logRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [lines])

  useEffect(() => {
    if (done) refresh()
  }, [done, refresh])

  const isActive = run ? !TERMINAL.includes(run.status) : false
  const totalTokens = run ? (run.tokens_in ?? 0) + (run.tokens_out ?? 0) || null : null

  return (
    <div>
      <PageHeader
        kicker={`Run ${shortId(runId)}`}
        title={run?.issue_title || "Run"}
        subtitle={run ? `${run.repo} #${run.issue_number}` : undefined}
        actions={
          <>
            <Button asChild variant="ghost">
              <Link to="/">
                <ArrowLeft /> Back
              </Link>
            </Button>
            {isActive && (
              <Button
                variant="outline"
                onClick={async () => {
                  await post(`/api/runs/${runId}/cancel`)
                  refresh()
                }}
              >
                Cancel run
              </Button>
            )}
          </>
        }
      />

      {error && <ErrorNote message={error} />}

      {run && (
        <Card className="mb-6 p-optical-lg">
          <div className="grid grid-cols-2 gap-x-6 gap-y-6 sm:grid-cols-4">
            <Field label="Status">
              <StateBadge state={runOutcome(run)} />
            </Field>
            {/* No issue behind a provisioning run — it warms the repo's golden. */}
            <Field label={run.issue_number > 0 ? "Issue" : "Repo"}>
              {run.repo}
              {run.issue_number > 0 && `#${run.issue_number}`}
            </Field>
            <Field label="Kind">{run.kind}</Field>
            <Field label="Branch">{run.branch || "—"}</Field>
            <Field label="Machine">{run.vm_name || "—"}</Field>
            <Field label={run.kind === "review" ? "Cycle" : "Attempt"}>{run.attempt ?? 1}</Field>
            <Field label="Duration">{duration(run)}</Field>
            <Field label="Tokens">{tokens(totalTokens)}</Field>
            <Field label="Cost">{cost(run.cost_usd)}</Field>
            <Field label="Exit">{run.exit_code ?? "—"}</Field>
            <Field label="Agent">{run.agent ?? "—"}</Field>
            <Field label="Pull request">
              {run.pr_url ? (
                <a
                  href={run.pr_url}
                  target="_blank"
                  rel="noreferrer"
                  className="text-primary underline-offset-4 hover:underline"
                >
                  view PR
                </a>
              ) : (
                "—"
              )}
            </Field>
          </div>
          {run.error && (
            <div className="mt-6 border border-border bg-bad/15 px-4 py-3 font-mono text-xs text-bad">
              {run.error}
            </div>
          )}
        </Card>
      )}

      {/* Everything below the run row. The `Cost` field above is what the runtime
          reported for the whole run; these are the calls it is made of, and they exist
          even when the run died before reporting anything. The two disagreeing slightly
          is expected — the runtime bills side calls that never reach the event stream. */}
      {trace && (trace.totals.calls ?? 0) > 0 && (
        <Card className="mb-6 p-optical-lg">
          <div className="grid grid-cols-2 gap-x-6 gap-y-6 sm:grid-cols-4">
            <Field label="Model calls">{trace.totals.calls}</Field>
            <Field label="Turns">{trace.totals.turns ?? "—"}</Field>
            <Field label="Cache read">{tokens(trace.totals.cache_read_tokens ?? null)}</Field>
            <Field label="Cache write">{tokens(trace.totals.cache_write_tokens ?? null)}</Field>
            <Field label="Output tokens">{tokens(trace.totals.output_tokens ?? null)}</Field>
            <Field label="Derived cost">{cost(trace.totals.derived_cost_usd ?? null)}</Field>
            <Field label="Models">{trace.by_model.map((m) => m.model).join(", ") || "—"}</Field>
            <Field label="Tool calls">
              {trace.tools.reduce((s, t) => s + t.calls, 0)}
              {trace.tools.some((t) => t.failures > 0) && (
                <span className="text-bad">
                  {" "}
                  ({trace.tools.reduce((s, t) => s + t.failures, 0)} failed)
                </span>
              )}
            </Field>
          </div>
          {trace.tools.length > 0 && (
            <div className="mt-7 border-t border-subtle pt-5">
              <SectionHead>Tool time</SectionHead>
              <div className="mt-2 space-y-2">
                {trace.tools.slice(0, 6).map((t) => (
                  <div key={t.tool} className="flex items-center gap-3">
                    <span className="w-24 shrink-0 font-mono text-xs">{t.tool}</span>
                    <Meter value={t.duration_ms} max={trace.tools[0].duration_ms} />
                    <span className="w-24 shrink-0 text-right font-mono text-xs tabular-nums text-muted-foreground">
                      {Math.round(t.duration_ms / 1000)}s · {t.calls}×
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </Card>
      )}

      <SectionHead>Agent output</SectionHead>
      {/* The log keeps its own ink ground in both themes — a terminal that turned to
          paper in light mode would stop reading as a terminal. */}
      <div
        ref={logRef}
        className="h-[60vh] overflow-y-auto border border-border bg-log p-4 font-mono text-[12.5px] leading-relaxed shadow-hard"
      >
        {lines.length === 0 ? (
          <span className="text-white/40">{done ? "no output" : "connecting…"}</span>
        ) : (
          lines.map((line, i) => (
            <div key={i} className={cn("whitespace-pre-wrap break-words", lineClass(line))}>
              {line}
            </div>
          ))
        )}
      </div>
    </div>
  )
}
