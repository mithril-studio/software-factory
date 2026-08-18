import { usePoll, type Project } from "@/lib/api"
import { Badge } from "@/components/ui/badge"
import { Card } from "@/components/ui/card"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { PageHeader, Empty, ErrorNote } from "@/components/Page"

function gitUrl(repo: string): string {
  return `https://github.com/${repo}`
}

/** What the last sweep found on the machine this repo's runs fork.
 *
 * The warm golden is what the prompt promises the agent ("dependencies are installed, do not
 * reinstall"), so drift here is a promise going quietly false — worth a badge, not a footnote.
 */
function Freshness({ p }: { p: Project }) {
  if (p.golden_error) return <Badge variant="bad">{p.golden_error}</Badge>
  if (!p.golden_checked_at) return <span className="text-muted-foreground">not checked yet</span>
  if (p.golden_stale_deps)
    return <Badge variant="bad">deps moved: {p.golden_stale_deps.trim().split(/\s+/).join(", ")}</Badge>
  if (p.golden_behind) return <Badge variant="warn">{p.golden_behind} behind</Badge>
  return <Badge variant="ok">current</Badge>
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
                <TableHead>Freshness</TableHead>
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
                  <TableCell className="font-mono text-muted-foreground">{p.golden}</TableCell>
                  <TableCell>
                    <Freshness p={p} />
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
