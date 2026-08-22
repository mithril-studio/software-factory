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
  // The two ways an *approved* review still does not end in a merge. Deliberately not red:
  // the reviewer verified the change and signed it off, so the problem is downstream of the
  // work — CI found something the run VM does not run, or the merge itself was refused.
  // Colouring these the same as a rejection is what made #77 look like a rejected change.
  "ci red": "warn",
  "not merged": "warn",
  // issue (plan) states
  done: "ok",
  blocked: "warn",
  none: "muted",
  error: "bad",
}

export function StateBadge({ state }: { state: string }) {
  return <Badge variant={VARIANT[state] ?? "default"}>{state}</Badge>
}
