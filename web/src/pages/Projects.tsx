import { usePoll, type Project } from "@/lib/api"
import { ago } from "@/lib/format"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { PageHeader, Empty, ErrorNote } from "@/components/Page"

function Stat({ label, value, tone }: { label: string; value: number; tone?: "ok" | "bad" | "warn" }) {
  const color = tone === "ok" ? "text-ok" : tone === "bad" ? "text-bad" : tone === "warn" ? "text-warn" : ""
  return (
    <div>
      <div className={`text-2xl font-semibold ${color}`}>{value}</div>
      <div className="text-xs text-muted-foreground">{label}</div>
    </div>
  )
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
      <div className="grid gap-4 sm:grid-cols-2">
        {projects?.map((p) => (
          <Card key={p.repo}>
            <CardHeader className="flex-row items-center justify-between">
              <CardTitle className="font-mono text-sm">{p.repo}</CardTitle>
              {p.active > 0 && <Badge variant="warn">{p.active} active</Badge>}
            </CardHeader>
            <CardContent>
              <div className="grid grid-cols-3 gap-4">
                <Stat label="runs" value={p.runs} />
                <Stat label="succeeded" value={p.succeeded} tone="ok" />
                <Stat label="failed" value={p.failed} tone="bad" />
              </div>
              <div className="mt-4 text-xs text-muted-foreground">last run {ago(p.last_run)}</div>
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  )
}
