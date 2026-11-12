import { Badge } from "@/components/ui/badge"

// Map a run status or an issue's factory state to a colour.
const VARIANT: Record<string, "ok" | "warn" | "bad" | "muted" | "default"> = {
  // run statuses
  succeeded: "ok",
  running: "warn",
  provisioning: "warn",
  forking: "warn", // rows written before v1
  queued: "muted",
  failed: "bad",
  cancelled: "bad",
  // issue states on the queue page
  done: "ok",
  none: "muted",
  blocked: "warn", // issues labelled before v1
  error: "bad",
}

/** The colour one state carries, for places that need the hue without the badge. */
export function stateVariant(state: string): "ok" | "warn" | "bad" | "muted" | "default" {
  return VARIANT[state] ?? "default"
}

export function StateBadge({ state }: { state: string }) {
  return <Badge variant={stateVariant(state)}>{state}</Badge>
}
