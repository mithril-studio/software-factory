import { useMemo, useRef, useState, useEffect } from "react"
import { Link } from "react-router-dom"
import { Filter, ChevronDown, Check } from "lucide-react"
import { usePoll, type Run } from "@/lib/api"
import { ago, cost, duration, shortId, tokens } from "@/lib/format"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Card } from "@/components/ui/card"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { StateBadge } from "@/components/StateBadge"
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

function RunRow({ run }: { run: Run }) {
  return (
    <TableRow>
      <TableCell>
        <div className="flex items-center gap-1.5">
          <StateBadge state={run.status} />
          {run.over_budget ? <Badge variant="warn">over budget</Badge> : null}
        </div>
      </TableCell>
      <TableCell>
        <Link to={`/runs/${run.id}`} className="font-mono text-primary underline-offset-4 hover:underline">
          {shortId(run.id)}
        </Link>
      </TableCell>
      <TableCell>
        <div className="flex flex-wrap items-center gap-x-2">
          <span className="font-mono">{run.repo}</span>
          {run.issue_number > 0 && (
            <span className="font-mono text-muted-foreground">#{run.issue_number}</span>
          )}
          {run.kind !== "build" && <Badge variant="outline">{run.kind}</Badge>}
        </div>
        <div className="max-w-80 truncate text-[11px] text-muted-foreground">{run.issue_title}</div>
        {run.status !== "succeeded" && run.error && (
          <div className="max-w-80 truncate text-[11px] text-bad">{run.error}</div>
        )}
      </TableCell>
      <TableCell className="font-mono tabular-nums">{duration(run)}</TableCell>
      <TableCell className="font-mono tabular-nums">
        {tokens((run.tokens_in ?? 0) + (run.tokens_out ?? 0) || null)}
      </TableCell>
      <TableCell className="font-mono tabular-nums">{cost(run.cost_usd)}</TableCell>
      <TableCell className="font-mono text-muted-foreground">{ago(run.started_at ?? run.created_at)}</TableCell>
      <TableCell>
        {run.pr_url ? (
          <a
            href={run.pr_url}
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
  )
}

export function Runs() {
  const { data: runs, error } = usePoll<Run[]>("/api/runs", 4000)
  const [selected, setSelected] = useState<Set<string>>(new Set())

  const projects = useMemo(() => Array.from(new Set((runs ?? []).map((r) => r.repo))).sort(), [runs])
  const filtered = useMemo(
    () => (runs ?? []).filter((r) => selected.size === 0 || selected.has(r.repo)),
    [runs, selected]
  )

  function toggle(repo: string) {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(repo)) next.delete(repo)
      else next.add(repo)
      return next
    })
  }

  return (
    <div>
      <PageHeader
        kicker="Dispatch log"
        title="Runs"
        subtitle="One row per run: an issue, a VM, an agent, a pull request."
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
      {runs && runs.length === 0 && <Empty>No runs yet. Label an issue `agent:queued` to start one.</Empty>}
      {runs && runs.length > 0 && filtered.length === 0 && <Empty>No runs match the selected projects.</Empty>}
      {filtered.length > 0 && (
        <Card>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Status</TableHead>
                <TableHead>Run</TableHead>
                <TableHead>Issue</TableHead>
                <TableHead>Duration</TableHead>
                <TableHead>Tokens</TableHead>
                <TableHead>Cost</TableHead>
                <TableHead>Started</TableHead>
                <TableHead>PR</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {filtered.map((r) => (
                <RunRow key={r.id} run={r} />
              ))}
            </TableBody>
          </Table>
        </Card>
      )}
    </div>
  )
}
