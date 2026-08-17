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
 */

export function Meter({ value, max }: { value: number; max: number }) {
  const pct = max > 0 ? Math.min(100, (value / max) * 100) : 0
  return (
    // The track is a dim step of the same hue rather than neutral grey, so an almost
    // empty bar still reads as "this measure, nearly none of it" at a glance.
    <div className="h-2 w-full overflow-hidden rounded-sm bg-primary/15">
      {/* Square at the baseline, 4px rounded at the data end. */}
      <div
        className="h-full rounded-r-[4px] bg-primary"
        style={{ width: `${Math.max(pct, value > 0 ? 1.5 : 0)}%` }}
      />
    </div>
  )
}

export function StatTile({
  label,
  value,
  note,
}: {
  label: string
  value: string
  note?: string
}) {
  return (
    <div className="rounded-xl border border-border bg-card px-4 py-3">
      <div className="text-xs text-muted-foreground">{label}</div>
      {/* Proportional figures, not tabular: these are standalone display numbers, and
          tabular widths make them look loose at this size. Columns get tabular-nums. */}
      <div className="mt-1 text-2xl font-semibold tracking-tight">{value}</div>
      {note && <div className="mt-0.5 text-xs text-muted-foreground">{note}</div>}
    </div>
  )
}
