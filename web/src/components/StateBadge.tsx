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
  // improvement-ledger states. `merged` is warn rather than ok on purpose: the change is
  // live and *ungraded*, which is a state that wants attention, not a green tick. `kept` is
  // the only outcome that earned one, and `reverted` is not a failure — it is the loop
  // deleting something that did not pay for itself, which is the half that keeps context
  // from growing forever.
  proposed: "muted",
  building: "warn",
  merged: "warn",
  kept: "ok",
  reverted: "muted",
  rejected: "bad",
  abandoned: "muted",
}

/** The colour one state carries, for the places that need the hue without the badge —
 *  the phase strip on the runs list. Exported so a second reading of "is this state bad?"
 *  cannot drift away from the badge's, which is how a rejected review and an approved one
 *  came to look alike in the first place. */
export function stateVariant(state: string): "ok" | "warn" | "bad" | "muted" | "default" {
  return VARIANT[state] ?? "default"
}

export function StateBadge({ state }: { state: string }) {
  return <Badge variant={stateVariant(state)}>{state}</Badge>
}
