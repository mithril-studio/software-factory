import { usePoll, type PlanIssue } from "@/lib/api"
import { Card } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { StateBadge } from "@/components/StateBadge"
import { PageHeader, Empty, ErrorNote } from "@/components/Page"

export function Plan() {
  const { data: issues, error } = usePoll<PlanIssue[]>("/api/plan", 15000)

  return (
    <div>
      <PageHeader
        title="Plan"
        subtitle="Open issues across watched repos, in the order the factory works them."
      />
      {error && <ErrorNote message={error} />}
      {issues && issues.length === 0 && (
        <Empty>No open issues. Label one `agent:queued` to enqueue it.</Empty>
      )}
      {issues && issues.length > 0 && (
        <Card>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>State</TableHead>
                <TableHead>Project</TableHead>
                <TableHead>#</TableHead>
                <TableHead>Title</TableHead>
                <TableHead>Labels</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {issues.map((i) => (
                <TableRow key={`${i.repo}#${i.number}`}>
                  <TableCell>
                    <StateBadge state={i.state} />
                  </TableCell>
                  <TableCell className="font-mono text-muted-foreground">{i.repo}</TableCell>
                  <TableCell className="font-mono">
                    {i.url ? (
                      <a href={i.url} target="_blank" rel="noreferrer" className="text-primary hover:underline">
                        {i.number}
                      </a>
                    ) : (
                      i.number
                    )}
                  </TableCell>
                  <TableCell className="max-w-md truncate">{i.title}</TableCell>
                  <TableCell>
                    <div className="flex flex-wrap gap-1">
                      {i.labels.map((l) => (
                        <Badge key={l} variant="outline">
                          {l}
                        </Badge>
                      ))}
                    </div>
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
