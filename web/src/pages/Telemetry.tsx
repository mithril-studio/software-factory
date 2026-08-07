import { BarChart3 } from "lucide-react"
import { PageHeader, Empty } from "@/components/Page"

export function Telemetry() {
  return (
    <div>
      <PageHeader title="Telemetry" subtitle="Runs, success rate, durations, token spend, context." />
      <Empty>
        <BarChart3 className="mx-auto mb-3 size-6" />
        Arrives with the telemetry layer — OTLP ingest of the agent's traces, keyed by run.id.
        <div className="mt-2 text-xs">Until then, per-run tokens and cost live on the Runs page.</div>
      </Empty>
    </div>
  )
}
