import { post, usePoll, type Agent } from "@/lib/api"
import { useState } from "react"
import { Boxes, RefreshCw } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { PageHeader, Empty, ErrorNote } from "@/components/Page"

export function Agents() {
  const { data: agents, error, refresh } = usePoll<Agent[]>("/api/agents", 15000)
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
    <div>
      <PageHeader
        title="Agents"
        subtitle="Boxd machines: the golden runs fork from, plus any live run VMs."
        actions={
          <Button variant="outline" onClick={reconcile} disabled={reconciling}>
            <RefreshCw /> {reconciling ? "Reconciling…" : "Reconcile"}
          </Button>
        }
      />
      {error && <ErrorNote message={error} />}
      {agents && agents.length === 0 && (
        <Empty>
          <Boxes className="mx-auto mb-2 size-6" />
          No machines. Check BOXD_API_KEY and the golden.
        </Empty>
      )}
      {agents && agents.length > 0 && (
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
              {agents.map((a) => (
                <TableRow key={a.name}>
                  <TableCell className="font-mono">{a.name}</TableCell>
                  <TableCell>
                    <Badge variant={a.role === "golden" ? "ok" : "muted"}>{a.role}</Badge>
                  </TableCell>
                  <TableCell className="text-muted-foreground">{a.status ?? "—"}</TableCell>
                  <TableCell className="text-right">
                    {a.is_golden && <Badge variant="outline">golden</Badge>}
                    {a.orphan && <Badge variant="bad">orphan</Badge>}
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
