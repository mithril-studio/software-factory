import { usePoll, type Project } from "@/lib/api"
import { Badge } from "@/components/ui/badge"
import { Card } from "@/components/ui/card"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { PageHeader, Empty, ErrorNote } from "@/components/Page"

function gitUrl(repo: string): string {
  return `https://github.com/${repo}`
}

export function Projects() {
  const { data: projects, error } = usePoll<Project[]>("/api/projects", 15000)

  return (
    <div>
      <PageHeader title="Projects" subtitle="Repos the factory watches for labelled issues." />
      {error && <ErrorNote message={error} />}
      {projects && projects.length === 0 && (
        <Empty>No repos configured. Set FACTORY_REPOS on the control plane.</Empty>
      )}
      {projects && projects.length > 0 && (
        <Card>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Id</TableHead>
                <TableHead>Git Url</TableHead>
                <TableHead>Golden</TableHead>
                <TableHead className="text-right">Runs</TableHead>
                <TableHead className="text-right">Successful Runs</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {projects.map((p) => (
                <TableRow key={p.repo}>
                  <TableCell className="font-mono">{p.repo}</TableCell>
                  <TableCell>
                    <a
                      href={gitUrl(p.repo)}
                      target="_blank"
                      rel="noreferrer"
                      className="font-mono text-primary hover:underline"
                    >
                      {gitUrl(p.repo)}
                    </a>
                  </TableCell>
                  <TableCell className="font-mono">
                    {p.golden || "—"}
                    {p.warm && (
                      <Badge variant="outline" className="ml-2">
                        warm
                      </Badge>
                    )}
                  </TableCell>
                  <TableCell className="text-right font-mono">{p.runs}</TableCell>
                  <TableCell className="text-right font-mono text-ok">{p.succeeded}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Card>
      )}
    </div>
  )
}
