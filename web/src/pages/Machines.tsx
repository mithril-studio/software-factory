import { post, usePoll, type Agent, type Machine } from "@/lib/api"
import { useState } from "react"
import { Boxes, RefreshCw } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { PageHeader, Empty, ErrorNote } from "@/components/Page"

/** One page, two questions that stopped having the same answer when goldens became snapshots:
 *  what this deployment *can* run, and what is running. The agents table is first because an
 *  empty one explains an empty second table, and never the other way round. */

/** Evidence, not a probe. A golden is graded by whether a run finished on it having produced
 *  usage — its credential is what expires, and using one is the only test that proves anything.
 *  So "unproven" is a real third state and must not read as broken. */
function Health({ a }: { a: Agent }) {
  if (a.error) return <Badge variant="bad">{a.error}</Badge>
  if (a.verified_at) return <Badge variant="ok">verified {a.verified_at.slice(0, 10)}</Badge>
  if (a.ok) return <Badge variant="muted">ran, no usage</Badge>
  return <span className="text-muted-foreground">unproven</span>
}

function AgentTable({ agents }: { agents: Agent[] }) {
  return (
    <Card>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Agent</TableHead>
            <TableHead>Snapshot</TableHead>
            <TableHead>Version</TableHead>
            <TableHead>Events</TableHead>
            <TableHead>Health</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {agents.map((a) => (
            <TableRow key={a.snapshot}>
              <TableCell className="font-mono">
                {a.agent}
                {a.default && (
                  <Badge variant="outline" className="ml-2">
                    default
                  </Badge>
                )}
              </TableCell>
              <TableCell className="font-mono text-muted-foreground">{a.snapshot}</TableCell>
              <TableCell className="font-mono text-muted-foreground">
                {a.version ?? "—"}
                {a.agent_version && ` · ${a.agent_version}`}
              </TableCell>
              <TableCell className="font-mono text-muted-foreground">{a.events ?? "—"}</TableCell>
              <TableCell>
                <Health a={a} />
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </Card>
  )
}

function MachineTable({ machines }: { machines: Machine[] }) {
  return (
    <Card>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Name</TableHead>
            <TableHead>Role</TableHead>
            <TableHead>Status</TableHead>
            <TableHead />
          </TableRow>
        </TableHeader>
        <TableBody>
          {machines.map((m) => (
            <TableRow key={m.name}>
              <TableCell className="font-mono">{m.name}</TableCell>
              <TableCell>
                <Badge variant={m.role === "other" ? "muted" : "ok"}>{m.role}</Badge>
              </TableCell>
              <TableCell className="font-mono text-muted-foreground">{m.status ?? "—"}</TableCell>
              <TableCell className="text-right">
                {m.orphan && <Badge variant="bad">orphan</Badge>}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </Card>
  )
}

export function Machines() {
  const { data: agents, error: agentsError } = usePoll<Agent[]>("/api/agents", 15000)
  const { data: machines, error, refresh } = usePoll<Machine[]>("/api/machines", 15000)
  const [reconciling, setReconciling] = useState(false)

  async function reconcile() {
    setReconciling(true)
    try {
      await post("/api/reconcile")
      await refresh()
    } finally {
      setReconciling(false)
    }
  }

  return (
    <div className="space-y-12">
      <div>
        <PageHeader
          kicker="Registry"
          title="Agents"
          subtitle="Golden snapshots this deployment can dispatch onto. The name is the whole registry."
        />
        {agentsError && <ErrorNote message={agentsError} />}
        {agents && agents.length === 0 && (
          <Empty>
            <Boxes className="mx-auto mb-2 size-6" />
            No goldens. Build one with scripts/build-golden.sh, or check BOXD_API_KEY.
          </Empty>
        )}
        {agents && agents.length > 0 && <AgentTable agents={agents} />}
      </div>

      <div>
        <PageHeader
          kicker="Fleet"
          title="Machines"
          subtitle="Boxd VMs: the machines runs are happening on, plus anything else in the fleet."
          actions={
            <Button variant="outline" onClick={reconcile} disabled={reconciling}>
              <RefreshCw /> {reconciling ? "Reconciling…" : "Reconcile"}
            </Button>
          }
        />
        {error && <ErrorNote message={error} />}
        {machines && machines.length === 0 && <Empty>No machines running.</Empty>}
        {machines && machines.length > 0 && <MachineTable machines={machines} />}
      </div>
    </div>
  )
}
