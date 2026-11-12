import { useCallback, useEffect, useState } from "react"
import { get, queueIssue, usePoll, type PlanIssue, type Project } from "@/lib/api"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { StateBadge } from "@/components/StateBadge"
import { PageHeader, Empty, ErrorNote, Loading } from "@/components/Page"

type Queue = { issues: PlanIssue[]; errors: string[] }

/** Every watched repo's open issues, read one repo at a time so a repo GitHub would not
 *  answer for shows as an error instead of as a repo with nothing open. */
function useQueue(repos: string[] | null, intervalMs: number) {
  const [queue, setQueue] = useState<Queue | null>(null)
  const key = repos?.join(",") ?? null

  const refresh = useCallback(async () => {
    if (key === null) return
    const names = key ? key.split(",") : []
    const answers = await Promise.allSettled(
      names.map((repo) => get<PlanIssue[]>(`/api/plan?repo=${encodeURIComponent(repo)}`))
    )
    const issues: PlanIssue[] = []
    const errors: string[] = []
    answers.forEach((a, i) => {
      if (a.status === "fulfilled") issues.push(...a.value)
      else errors.push(a.reason instanceof Error ? a.reason.message : `could not read ${names[i]}`)
    })
    setQueue({ issues, errors })
  }, [key])

  useEffect(() => {
    refresh()
    const id = setInterval(refresh, intervalMs)
    return () => clearInterval(id)
  }, [refresh, intervalMs])

  return { queue, refresh }
}

/** Queue an issue that is not in the factory yet, or retry one that failed. */
function QueueButton({ issue, onQueued }: { issue: PlanIssue; onQueued: () => void }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  if (issue.state !== "none" && issue.state !== "failed") return null

  async function onClick() {
    setBusy(true)
    setError(null)
    try {
      await queueIssue(issue.repo, issue.number)
      onQueued()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      setBusy(false)
    }
  }

  return (
    <div className="flex items-center justify-end gap-2">
      {error && <span className="max-w-48 truncate font-mono text-[10px] text-bad" title={error}>{error}</span>}
      <Button variant="outline" size="sm" disabled={busy} onClick={onClick}>
        {busy ? "Queueing…" : issue.state === "failed" ? "Retry" : "Queue"}
      </Button>
    </div>
  )
}

export function Plan() {
  const { data: projects, error: projectsError } = usePoll<Project[]>("/api/projects", 60000)
  const repos = projects ? projects.map((p) => p.repo) : null
  const { queue, refresh } = useQueue(repos, 15000)
  const issues = queue?.issues ?? []

  return (
    <div>
      <PageHeader
        kicker="Work"
        title="Queue"
        subtitle="Open issues in the repos the factory watches. Queue one and its run starts within a minute; each repo works one issue at a time, lowest number first."
      />
      {projectsError && <ErrorNote message={projectsError} />}
      {queue?.errors.map((e) => <ErrorNote key={e} message={e} />)}
      {!queue && !projectsError && <Loading />}
      {projects && projects.length === 0 && <Empty>No repos connected. Connect one on Projects.</Empty>}
      {queue && issues.length === 0 && queue.errors.length === 0 && projects && projects.length > 0 && (
        <Empty>No open issues in the watched repos.</Empty>
      )}
      {issues.length > 0 && (
        <Card>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>State</TableHead>
                <TableHead>Project</TableHead>
                <TableHead>#</TableHead>
                <TableHead>Title</TableHead>
                <TableHead>Labels</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {issues.map((i) => (
                <TableRow key={`${i.repo}#${i.number}`}>
                  <TableCell>
                    {i.state === "none" ? (
                      <span className="text-muted-foreground">—</span>
                    ) : (
                      <StateBadge state={i.state} />
                    )}
                  </TableCell>
                  <TableCell className="font-mono">{i.repo}</TableCell>
                  <TableCell className="font-mono">
                    <a
                      href={i.url}
                      target="_blank"
                      rel="noreferrer"
                      className="text-primary underline-offset-4 hover:underline"
                    >
                      {i.number}
                    </a>
                  </TableCell>
                  <TableCell className="max-w-md truncate text-[13px]">{i.title}</TableCell>
                  <TableCell>
                    <div className="flex flex-wrap gap-1">
                      {i.labels.map((l) => (
                        <Badge key={l} variant="outline">
                          {l}
                        </Badge>
                      ))}
                    </div>
                  </TableCell>
                  <TableCell>
                    <QueueButton issue={i} onQueued={refresh} />
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
