import { cn } from "@/lib/utils"

/**
 * A row-level proportional bar, and the stat tile the KPI row is built from.
 *
 * Deliberately one hue rather than a categorical palette. Every figure on the
 * Telemetry page is a magnitude compared against its siblings — "cache reads are most
 * of the bill", "Playwright is most of the wall clock" — and magnitude is a sequential
 * job, not an identity one. Identity comes from the row label beside the bar, which
 * reads more precisely than four colors ever could and stays legible under any colour
 * vision, in print, and in forced-colors mode. It also keeps the status hues (ok / warn
 * / bad) meaning run state and nothing else.
 *
 * Brutalist rendering: the track is a black-framed box and the fill is a solid terracotta
 * block that runs square into both ends. No rounding, no gradient — the bar is a
 * measured length, and anything soft at its tip lies about where the measurement stops.
 */
export function Meter({ value, max }: { value: number; max: number }) {
  const pct = max > 0 ? Math.min(100, (value / max) * 100) : 0
  return (
    <div className="h-2.5 w-full overflow-hidden border border-border bg-muted">
      <div
        className="h-full bg-primary"
        style={{ width: `${Math.max(pct, value > 0 ? 2 : 0)}%` }}
      />
    </div>
  )
}

/**
 * `featured` is the asymmetry lever: the tile that leads the KPI row spans wider and
 * sets its figure larger, so the row has a subject instead of four equal claims.
 */
export function StatTile({
  label,
  value,
  note,
  featured = false,
  className,
}: {
  label: string
  value: string
  note?: string
  featured?: boolean
  className?: string
}) {
  return (
    <div
      className={cn(
        "flex flex-col justify-between border border-border bg-card p-optical",
        featured ? "shadow-hard-accent" : "shadow-hard",
        className
      )}
    >
      <div className="eyebrow text-muted-foreground">{label}</div>
      {/* Display figures are set in the serif: at this size the numerals are the
          headline of the tile, and mono at 3rem reads as a code listing. The mono
          voice stays on the label and the footnote, where it belongs. */}
      <div
        className={cn(
          "mt-4 font-serif leading-none text-foreground",
          featured ? "text-7xl" : "text-4xl"
        )}
      >
        {value}
      </div>
      {note && <div className="mt-2 font-mono text-[10px] text-muted-foreground">{note}</div>}
    </div>
  )
}
