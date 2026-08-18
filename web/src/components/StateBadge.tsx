import { Badge } from "@/components/ui/badge"

// Map a run status or an issue's factory state to a colour.
const VARIANT: Record<string, "ok" | "warn" | "bad" | "muted" | "default"> = {
  // run statuses
  succeeded: "ok",
  running: "warn",
  forking: "warn",
  queued: "muted",
  failed: "bad",
  cancelled: "bad",
  // A review that ran fine and refused the change. Its run status is `succeeded`; the
  // outcome is not, and the list is where that difference has to be visible.
  "changes requested": "bad",
  // issue (plan) states
  done: "ok",
  blocked: "warn",
  none: "muted",
  error: "bad",
}

export function StateBadge({ state }: { state: string }) {
  return <Badge variant={VARIANT[state] ?? "default"}>{state}</Badge>
}
