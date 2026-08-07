import { useEffect, useRef } from "react"
import { Link, useParams } from "react-router-dom"
import { ArrowLeft } from "lucide-react"
import { post, usePoll, useRunLog, TERMINAL, type Run } from "@/lib/api"
import { cost, duration, shortId, tokens } from "@/lib/format"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { StateBadge } from "@/components/StateBadge"
import { PageHeader, ErrorNote } from "@/components/Page"
import { cn } from "@/lib/utils"

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
        {label}
      </div>
      <div className="mt-1 font-mono text-sm">{children}</div>
    </div>
  )
}

function lineClass(line: string): string {
  if (line.startsWith("[factory]")) return "text-primary"
  if (line.startsWith("[tool]")) return "text-warn"
  if (line.startsWith("[agent]")) return "text-ok"
  if (line.startsWith("[stderr]")) return "text-bad"
  return "text-foreground/80"
}

export function RunDetail() {
  const { runId = "" } = useParams()
  const { data: run, error, refresh } = usePoll<Run>(`/api/runs/${runId}`, 3000)
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
        title={run?.issue_title || "Run"}
        subtitle={<span className="font-mono">{shortId(runId)}</span>}
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
        <Card className="mb-6 p-5">
          <div className="grid grid-cols-2 gap-5 sm:grid-cols-4">
            <Field label="Status">
              <StateBadge state={run.status} />
            </Field>
            <Field label="Issue">
              {run.repo}#{run.issue_number}
            </Field>
            <Field label="Branch">{run.branch || "—"}</Field>
            <Field label="Machine">{run.vm_name || "—"}</Field>
            <Field label="Attempt">{run.attempt ?? 1}</Field>
            <Field label="Duration">{duration(run)}</Field>
            <Field label="Tokens">{tokens(totalTokens)}</Field>
            <Field label="Cost">{cost(run.cost_usd)}</Field>
            <Field label="Exit">{run.exit_code ?? "—"}</Field>
            <Field label="Agent">{run.agent ?? "—"}</Field>
            <Field label="Pull request">
              {run.pr_url ? (
                <a href={run.pr_url} target="_blank" rel="noreferrer" className="text-primary hover:underline">
                  view PR
                </a>
              ) : (
                "—"
              )}
            </Field>
          </div>
          {run.error && <div className="mt-4 font-mono text-sm text-bad">{run.error}</div>}
        </Card>
      )}

      <h2 className="mb-3 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        Agent output
      </h2>
      <div
        ref={logRef}
        className="h-[60vh] overflow-y-auto rounded-xl border border-border bg-[oklch(0.13_0.006_285.9)] p-4 font-mono text-[12.5px] leading-relaxed"
      >
        {lines.length === 0 ? (
          <span className="text-muted-foreground">{done ? "no output" : "connecting…"}</span>
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
