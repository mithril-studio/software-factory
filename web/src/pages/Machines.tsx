import { post, usePoll, type Golden, type Machine } from "@/lib/api"
import { useState } from "react"
import { Boxes, RefreshCw } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { PageHeader, Empty, ErrorNote } from "@/components/Page"

/** What this deployment can boot, and what is running. The goldens table is first because an
 *  empty one explains an empty second table, and never the other way round. */

function GoldenTable({ goldens }: { goldens: Golden[] }) {
  return (
    <Card>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Snapshot</TableHead>
            <TableHead>Repo</TableHead>
            <TableHead>Version</TableHead>
            <TableHead>State</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {goldens.map((g) => {
            const base = g.repo === ""
            return (
              <TableRow key={g.name}>
                <TableCell className="font-mono">
                  {g.name}
                  {base && (
                    <Badge variant="outline" className="ml-2">
                      base
                    </Badge>
                  )}
                </TableCell>
                <TableCell className="font-mono text-muted-foreground">
                  {base ? "— every repo without one of its own" : g.repo}
                </TableCell>
                <TableCell className="font-mono text-muted-foreground">{g.version || "—"}</TableCell>
                <TableCell>
                  {/* A capture with no version yet cannot be booted at all. */}
                  {g.version ? (
                    <Badge variant="ok">{g.status || "ready"}</Badge>
                  ) : (
                    <Badge variant="muted">capturing…</Badge>
                  )}
                </TableCell>
              </TableRow>
            )
          })}
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
  const { data: goldens, error: goldensError } = usePoll<Golden[]>("/api/goldens", 15000)
  const { data: machines, error, refresh } = usePoll<Machine[]>("/api/machines", 15000)
  const [reconciling, setReconciling] = useState(false)
  const [reconcileError, setReconcileError] = useState<string | null>(null)

  async function reconcile() {
    setReconciling(true)
    setReconcileError(null)
    try {
      await post("/api/reconcile")
      await refresh()
    } catch (e) {
      setReconcileError(e instanceof Error ? e.message : String(e))
    } finally {
      setReconciling(false)
    }
  }

  return (
    <div className="space-y-12">
      <div>
        <PageHeader
          kicker="Registry"
          title="Goldens"
          subtitle="Snapshots this deployment can boot: the base every repo falls back to, plus any warm ones built with scripts/refresh-golden.sh."
        />
        {goldensError && <ErrorNote message={goldensError} />}
        {goldens && goldens.length === 0 && (
          <Empty>
            <Boxes className="mx-auto mb-2 size-6" />
            No goldens. Build the base with scripts/build-golden.sh, or check BOXD_API_KEY.
          </Empty>
        )}
        {goldens && goldens.length > 0 && <GoldenTable goldens={goldens} />}
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
        {reconcileError && <ErrorNote message={`Reconcile failed: ${reconcileError}`} />}
        {machines && machines.length === 0 && <Empty>No machines running.</Empty>}
        {machines && machines.length > 0 && <MachineTable machines={machines} />}
      </div>
    </div>
  )
}
