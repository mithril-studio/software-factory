import { Check as Tick, TriangleAlert, X } from "lucide-react"
import type { Check } from "@/lib/api"
import { cn } from "@/lib/utils"

/** Preflight's answers, rendered the way `control.preflight.report` prints them.
 *
 *  Three states, not two, and the third is the point: a failing check that is not `fatal` is a
 *  warning — a repo with no `.factory.md` still builds, it just builds with less context.
 *  Showing warnings in the same red as blockers is how a checklist stops being read, and this
 *  panel is the one place somebody decides whether to connect a repo.
 *
 *  Each row is boxed rather than bulleted: at a glance the reader is counting how many blocks
 *  are red, and a framed row makes that count survive peripheral vision. The check's name is
 *  the app's label voice (uppercase mono); its detail is prose and stays in Inter.
 */
const TONE = {
  ok: { icon: Tick, text: "text-ok", edge: "border-subtle" },
  warn: { icon: TriangleAlert, text: "text-warn", edge: "border-warn/60" },
  bad: { icon: X, text: "text-bad", edge: "border-bad/70" },
} as const

export function CheckList({ checks }: { checks: Check[] }) {
  return (
    <ul className="flex flex-col gap-1.5">
      {checks.map((c) => {
        const { icon: Icon, text, edge } = TONE[c.ok ? "ok" : c.fatal ? "bad" : "warn"]
        return (
          <li
            key={c.name}
            className={cn("flex items-start gap-2.5 border bg-card px-3 py-2 text-sm", edge)}
          >
            <Icon className={cn("mt-0.5 size-3.5 shrink-0", text)} strokeWidth={2.5} />
            <span>
              <span className="eyebrow text-foreground">{c.name}</span>
              <span className="text-muted-foreground"> — {c.detail}</span>
            </span>
          </li>
        )
      })}
    </ul>
  )
}
