import { useState } from "react"
import { useNavigate, Link } from "react-router-dom"
import { Play } from "lucide-react"
import { post, usePoll, type Run } from "@/lib/api"
import { ago, cost, duration, shortId, tokens } from "@/lib/format"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { StateBadge } from "@/components/StateBadge"
import { PageHeader, Empty, ErrorNote } from "@/components/Page"

function StartRun() {
  const navigate = useNavigate()
  const [repo, setRepo] = useState("")
  const [issue, setIssue] = useState("")
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  async function start() {
    setBusy(true)
    setErr(null)
    try {
      const { run_id } = await post<{ run_id: string }>("/api/runs", {
        repo: repo.trim(),
        issue_number: Number(issue),
      })
      navigate(`/runs/${run_id}`)
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
      setBusy(false)
    }
  }

  return (
    <Card className="mb-6 p-4">
      <div className="flex flex-wrap items-center gap-3">
        <input
          className="h-9 min-w-56 flex-1 rounded-md border border-input bg-background px-3 text-sm outline-none focus:ring-2 focus:ring-ring"
          placeholder="owner/repo"
          value={repo}
          onChange={(e) => setRepo(e.target.value)}
        />
        <input
          className="h-9 w-28 rounded-md border border-input bg-background px-3 text-sm outline-none focus:ring-2 focus:ring-ring"
          placeholder="issue #"
          type="number"
          value={issue}
          onChange={(e) => setIssue(e.target.value)}
        />
        <Button onClick={start} disabled={busy || !repo || !issue}>
          <Play /> {busy ? "Starting…" : "Start run"}
        </Button>
      </div>
      {err && (
        <div className="mt-3">
          <ErrorNote message={err} />
        </div>
      )}
    </Card>
  )
}

export function Runs() {
  const { data: runs, error } = usePoll<Run[]>("/api/runs", 4000)

  return (
    <div>
      <PageHeader title="Runs" subtitle="Every dispatch — issue in, pull request out." />
      <StartRun />
      {error && <ErrorNote message={error} />}
      {runs && runs.length === 0 && <Empty>No runs yet. Start one above, or label an issue.</Empty>}
      {runs && runs.length > 0 && (
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
              {runs.map((r) => (
                <TableRow key={r.id}>
                  <TableCell>
                    <div className="flex items-center gap-2">
                      <StateBadge state={r.status} />
                      {r.attempt && r.attempt > 1 && (
                        <span className="text-xs text-muted-foreground">try {r.attempt}</span>
                      )}
                    </div>
                  </TableCell>
                  <TableCell>
                    <Link to={`/runs/${r.id}`} className="font-mono text-sm text-primary hover:underline">
                      {shortId(r.id)}
                    </Link>
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">{r.agent ?? "—"}</TableCell>
                  <TableCell>
                    <Link to={`/runs/${r.id}`} className="hover:underline">
                      <span className="font-mono text-sm">{r.repo}</span>
                      <span className="text-muted-foreground"> #{r.issue_number}</span>
                    </Link>
                    <div className="max-w-72 truncate text-xs text-muted-foreground">
                      {r.issue_title}
                    </div>
                  </TableCell>
                  <TableCell className="font-mono text-sm text-muted-foreground">
                    {duration(r)}
                  </TableCell>
                  <TableCell className="font-mono text-sm text-muted-foreground">
                    {tokens((r.tokens_in ?? 0) + (r.tokens_out ?? 0) || null)}
                  </TableCell>
                  <TableCell className="font-mono text-sm text-muted-foreground">
                    {cost(r.cost_usd)}
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    {ago(r.started_at ?? r.created_at)}
                  </TableCell>
                  <TableCell>
                    {r.pr_url ? (
                      <a
                        href={r.pr_url}
                        target="_blank"
                        rel="noreferrer"
                        className="text-sm text-primary hover:underline"
                      >
                        open
                      </a>
                    ) : (
                      <span className="text-muted-foreground">—</span>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Card>
      )}
    </div>
  )
}
