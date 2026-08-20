import { Check as Tick, TriangleAlert, X } from "lucide-react"
import type { Check } from "@/lib/api"

/** Preflight's answers, rendered the way `control.preflight.report` prints them.
 *
 *  Three states, not two, and the third is the point: a failing check that is not `fatal` is a
 *  warning — a repo with no `.factory.md` still builds, it just builds with less context.
 *  Showing warnings in the same red as blockers is how a checklist stops being read, and this
 *  panel is the one place somebody decides whether to connect a repo. */
export function CheckList({ checks }: { checks: Check[] }) {
  return (
    <ul className="flex flex-col gap-2">
      {checks.map((c) => {
        const state = c.ok ? "ok" : c.fatal ? "bad" : "warn"
        const Icon = state === "ok" ? Tick : state === "bad" ? X : TriangleAlert
        return (
          <li key={c.name} className="flex items-start gap-2.5 text-sm">
            <Icon
              className={
                "mt-0.5 size-4 shrink-0 " +
                (state === "ok"
                  ? "text-ok"
                  : state === "bad"
                    ? "text-bad"
                    : "text-warn")
              }
            />
            <span>
              <span className="font-medium text-foreground">{c.name}</span>
              <span className="text-muted-foreground"> — {c.detail}</span>
            </span>
          </li>
        )
      })}
    </ul>
  )
}
