import { useMemo, useRef, useState, useEffect } from "react"
import { Link } from "react-router-dom"
import { Filter, ChevronDown, ChevronRight, Check } from "lucide-react"
import {
  usePoll,
  runOutcome,
  attemptOutcome,
  attemptTotal,
  attemptPr,
  type Attempt,
  type Run,
} from "@/lib/api"
import { ago, cost, duration, shortId, tokens } from "@/lib/format"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Card } from "@/components/ui/card"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { StateBadge, stateVariant } from "@/components/StateBadge"
import { PageHeader, Empty, ErrorNote } from "@/components/Page"

function ProjectFilter({
  projects,
  selected,
  onToggle,
  onClear,
}: {
  projects: string[]
  selected: Set<string>
  onToggle: (repo: string) => void
  onClear: () => void
}) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    function onDown(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener("mousedown", onDown)
    return () => document.removeEventListener("mousedown", onDown)
  }, [open])

  return (
    <div className="relative" ref={ref}>
      <Button variant="outline" onClick={() => setOpen((o) => !o)}>
        <Filter />
        Filter
        {selected.size > 0 && (
          <span className="border border-border bg-primary px-1.5 text-[10px] text-primary-foreground">
            {selected.size}
          </span>
        )}
        <ChevronDown className={cn("transition-transform", open && "rotate-180")} />
      </Button>
      {open && (
        <div className="absolute right-0 z-20 mt-2 w-64 border border-border bg-popover p-1.5 shadow-hard">
          <div className="flex items-center justify-between px-2 py-1.5">
            <span className="eyebrow text-muted-foreground">Projects</span>
            {selected.size > 0 && (
              <button
                onClick={onClear}
                className="font-mono text-[10px] uppercase tracking-wider text-primary hover:underline"
                type="button"
              >
                Clear
              </button>
            )}
          </div>
          <div className="max-h-72 overflow-y-auto">
            {projects.length === 0 && (
              <div className="px-2 py-2 font-mono text-xs text-muted-foreground">No projects</div>
            )}
            {projects.map((repo) => {
              const on = selected.has(repo)
              return (
                <button
                  key={repo}
                  type="button"
                  onClick={() => onToggle(repo)}
                  className="flex w-full items-center gap-2 px-2 py-1.5 text-left text-sm transition-colors hover:bg-secondary"
                >
                  <span
                    className={cn(
                      "flex size-4 items-center justify-center border border-border",
                      on ? "bg-primary text-primary-foreground" : "bg-card"
                    )}
                  >
                    {on && <Check className="size-3" strokeWidth={3} />}
                  </span>
                  <span className="truncate font-mono text-xs">{repo}</span>
                </button>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}

/** One phase's contribution to the strip under an attempt's title: `build ✓ · ci ✗ · review ✓`.
 *  The point of it is that the failure is legible without expanding anything — which is the
 *  whole complaint about the old log, where a red CI was a fact you could only reach by
 *  opening the review that mentioned it in passing. */
const MARK_TONE: Record<string, string> = {
  ok: "text-ok",
  warn: "text-warn",
  bad: "text-bad",
  muted: "text-muted-foreground",
  default: "text-muted-foreground",
}

// "ci red" and "not merged" on a *review* say the reviewer approved and something after it
// did not go through. Before CI had a phase of its own that tag was the only place the
// failure was recorded, so it had to be the review's outcome. Now that CI carries its own
// mark, crossing the review out too would report one failure twice and blame it on the one
// step that did its job — the exact confusion this page exists to remove.
function phaseWentWrong(run: Run, outcome: string): boolean {
  if (run.kind === "review" && (outcome === "ci red" || outcome === "not merged")) return false
  const variant = stateVariant(outcome)
  return variant === "bad" || variant === "warn"
}

function PhaseMark({ run }: { run: Run }) {
  const outcome = runOutcome(run)
  const wrong = phaseWentWrong(run, outcome)
  const tone = wrong ? MARK_TONE[stateVariant(outcome)] : MARK_TONE.muted
  return (
    <span
      className={cn("font-mono text-[10px]", tone)}
      title={`${run.kind}: ${outcome}${run.error ? ` — ${run.error}` : ""}`}
    >
      {run.kind} {wrong ? "✗" : run.status === "succeeded" ? "✓" : "·"}
    </span>
  )
}

function PhaseRow({ run }: { run: Run }) {
  return (
    <TableRow className="bg-muted/30">
      <TableCell className="py-1.5 pl-10">
        <StateBadge state={runOutcome(run)} />
      </TableCell>
      <TableCell className="py-1.5">
        <Link
          to={`/runs/${run.id}`}
          className="font-mono text-primary underline-offset-4 hover:underline"
        >
          {shortId(run.id)}
        </Link>
      </TableCell>
      <TableCell className="py-1.5 font-mono text-muted-foreground">{run.agent ?? "—"}</TableCell>
      <TableCell className="py-1.5">
        <div className="flex items-center gap-1.5">
          <Badge variant="outline">{run.kind}</Badge>
          {run.attempt != null && run.attempt > 1 && (
            <span className="font-mono text-[10px] text-muted-foreground">try {run.attempt}</span>
          )}
        </div>
        {run.error && (
          <div className="max-w-96 truncate text-[11px] text-muted-foreground">{run.error}</div>
        )}
      </TableCell>
      <TableCell className="py-1.5 font-mono tabular-nums">{duration(run)}</TableCell>
      <TableCell className="py-1.5 font-mono tabular-nums">
        {tokens((run.tokens_in ?? 0) + (run.tokens_out ?? 0) || null)}
      </TableCell>
      <TableCell className="py-1.5 font-mono tabular-nums">{cost(run.cost_usd)}</TableCell>
      <TableCell className="py-1.5" />
      <TableCell className="py-1.5" />
    </TableRow>
  )
}

function AttemptRow({ attempt }: { attempt: Attempt }) {
  const [open, setOpen] = useState(false)
  const first = attempt.phases[0]
  const last = attempt.phases[attempt.phases.length - 1]
  const outcome = attemptOutcome(attempt)
  const pr = attemptPr(attempt)
  // Wall clock across the whole attempt, not the sum of its phases. The gaps between them —
  // waiting for a machine, waiting for CI — are time the issue spent unfinished, and the sum
  // quietly hides exactly the waits worth seeing.
  const elapsed = duration({ started_at: first.started_at, finished_at: last.finished_at })

  return (
    <>
      <TableRow>
        <TableCell>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => setOpen((o) => !o)}
              aria-expanded={open}
              aria-label={open ? "Hide phases" : "Show phases"}
              className="text-muted-foreground hover:text-foreground"
            >
              <ChevronRight className={cn("size-3.5 transition-transform", open && "rotate-90")} />
            </button>
            <StateBadge state={outcome} />
            {first.cycle != null && first.cycle > 1 && (
              <span className="font-mono text-[10px] text-muted-foreground">
                cycle {first.cycle}
              </span>
            )}
          </div>
        </TableCell>
        <TableCell>
          <button
            type="button"
            onClick={() => setOpen((o) => !o)}
            className="font-mono text-muted-foreground hover:text-foreground"
          >
            {attempt.phases.length} phase{attempt.phases.length === 1 ? "" : "s"}
          </button>
        </TableCell>
        <TableCell className="font-mono text-muted-foreground">
          {/* The last phase that ran on a machine. Reading `last` alone shows an em dash for
              every attempt CI judged, because a CI phase has no agent to report. */}
          {[...attempt.phases].reverse().find((p) => p.agent)?.agent ?? "—"}
        </TableCell>
        <TableCell>
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <span className="font-mono">{first.repo}</span>
            {/* A provisioning run has no issue behind it — it warms this repo's golden — so
                `#0` would be a reference to nothing. */}
            {first.issue_number > 0 && (
              <span className="font-mono text-muted-foreground">#{first.issue_number}</span>
            )}
            {attempt.phases.map((p, i) => (
              <span key={p.id} className="flex items-center gap-2">
                {i > 0 && <span className="text-[10px] text-muted-foreground">·</span>}
                <PhaseMark run={p} />
              </span>
            ))}
          </div>
          <div className="max-w-72 truncate text-[11px] text-muted-foreground">
            {first.issue_title}
          </div>
        </TableCell>
        <TableCell className="font-mono tabular-nums">{elapsed}</TableCell>
        <TableCell className="font-mono tabular-nums">
          {tokens(attemptTotal(attempt, (r) => (r.tokens_in ?? 0) + (r.tokens_out ?? 0) || null))}
        </TableCell>
        <TableCell className="font-mono tabular-nums">
          {cost(attemptTotal(attempt, (r) => r.cost_usd))}
        </TableCell>
        <TableCell className="font-mono text-muted-foreground">
          {ago(first.started_at ?? first.created_at)}
        </TableCell>
        <TableCell>
          {pr ? (
            <a
              href={pr}
              target="_blank"
              rel="noreferrer"
              className="font-mono text-primary underline-offset-4 hover:underline"
            >
              open
            </a>
          ) : (
            <span className="text-muted-foreground">—</span>
          )}
        </TableCell>
      </TableRow>
      {open && attempt.phases.map((p) => <PhaseRow key={p.id} run={p} />)}
    </>
  )
}

export function Runs() {
  const { data: attempts, error } = usePoll<Attempt[]>("/api/attempts", 4000)
  const [selected, setSelected] = useState<Set<string>>(new Set())

  const projects = useMemo(
    () => Array.from(new Set((attempts ?? []).map((a) => a.phases[0].repo))).sort(),
    [attempts]
  )

  const filtered = useMemo(
    () =>
      (attempts ?? []).filter(
        (a) => selected.size === 0 || selected.has(a.phases[0].repo)
      ),
    [attempts, selected]
  )

  function toggle(repo: string) {
    setSelected((prev) => {
      const next = new Set(prev)
      next.has(repo) ? next.delete(repo) : next.add(repo)
      return next
    })
  }

  return (
    <div>
      <PageHeader
        kicker="Dispatch log"
        title="Runs"
        subtitle="Every attempt. Open one to see its phases."
        actions={
          <ProjectFilter
            projects={projects}
            selected={selected}
            onToggle={toggle}
            onClear={() => setSelected(new Set())}
          />
        }
      />
      {error && <ErrorNote message={error} />}
      {attempts && attempts.length === 0 && (
        <Empty>No runs yet. Label an issue to start one.</Empty>
      )}
      {attempts && attempts.length > 0 && filtered.length === 0 && (
        <Empty>No runs match the selected projects.</Empty>
      )}
      {filtered.length > 0 && (
        <Card>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Status</TableHead>
                <TableHead>Run</TableHead>
                <TableHead>Agent</TableHead>
                <TableHead>Project</TableHead>
                <TableHead>Duration</TableHead>
                <TableHead>Tokens</TableHead>
                <TableHead>Cost</TableHead>
                <TableHead>Started</TableHead>
                <TableHead>PR</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {filtered.map((a) => (
                <AttemptRow key={a.key} attempt={a} />
              ))}
            </TableBody>
          </Table>
        </Card>
      )}
    </div>
  )
}
