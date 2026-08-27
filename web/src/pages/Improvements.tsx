import { Sprout } from "lucide-react"
import { usePoll, type Improvement } from "@/lib/api"
import { Card } from "@/components/ui/card"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { PageHeader, Empty, ErrorNote } from "@/components/Page"
import { StateBadge } from "@/components/StateBadge"
import { StatTile } from "@/components/Meter"

/** Ledger order, not alphabetical: this is the life of a proposal, and reading it in
 *  sequence is what makes a pile-up at one stage visible. */
const ORDER: Improvement["status"][] = [
  "proposed", "building", "merged", "kept", "reverted", "rejected", "abandoned",
]

/** What the change was, in two words. The artifact is the part worth scanning for — a
 *  column of `factory_md` edits is the loop putting weight on every future prompt. */
const ARTIFACT: Record<Improvement["artifact"], string> = {
  skill: "skill",
  factory_md: ".factory.md",
  mem: ".mem/",
  candidate: "candidate",
  harness: "harness",
  // The work order itself — the loop concluding that the issue was the defect rather than
  // the agent that worked on it. Worth spotting in a scan: it is the only row here whose fix
  // improves every issue written after it, rather than one repo's context.
  compose: "work order",
}

function num(v: number | null): string {
  return v === null || v === undefined ? "—" : String(Math.round(v * 1000) / 1000)
}

/** Did the number go the right way? Every metric in `LEARN_METRICS` is one where lower is
 *  better — rejection rate, ship-nothing rate, retry rate, cost per shipped issue — so one
 *  comparison covers all of them. Kept as a named function rather than inlined so that
 *  stops being true loudly rather than quietly if a metric is ever added where it is not. */
function moved(baseline: number | null, observed: number | null): string {
  if (baseline === null || observed === null) return ""
  const delta = observed - baseline
  if (Math.abs(delta) < 1e-9) return "no change"
  return delta < 0 ? `↓ ${num(Math.abs(delta))}` : `↑ ${num(delta)}`
}

export function Improvements() {
  const { data, error } = usePoll<Improvement[]>("/api/improvements", 30000)
  const rows = data ?? []

  const byStatus = (s: Improvement["status"]) => rows.filter((r) => r.status === s).length
  // The number that matters most on this page. A change sitting in `merged` is live and
  // ungraded — it is already being paid for on every run and nobody has established that it
  // was worth it. A growing count here is the loop failing to close its own loop.
  const ungraded = byStatus("merged")
  const kept = byStatus("kept")
  const reverted = byStatus("reverted")

  const sorted = [...rows].sort(
    (a, b) => ORDER.indexOf(a.status) - ORDER.indexOf(b.status) ||
      b.created_at.localeCompare(a.created_at),
  )

  return (
    <>
      <PageHeader
        kicker="Improvement loop"
        title="What it changed"
        subtitle={
          <>
            Every change the loop proposed, the runs it cited, and the number it promised to
            move. A merged row is live and still unproven; <em>reverted</em> is not a failure
            but the loop deleting something that did not pay for itself.
          </>
        }
      />

      {error && <ErrorNote message={error} />}

      {rows.length === 0 ? (
        <Empty>
          Nothing filed yet. The loop is off unless FACTORY_LEARN=1, and it waits for a repo
          to finish FACTORY_LEARN_EVERY issues before it reads anything.
        </Empty>
      ) : (
        <>
          <div className="mb-6 grid grid-cols-2 gap-3 sm:grid-cols-4">
            <StatTile label="Proposed" value={String(rows.length)} />
            <StatTile label="Live, ungraded" value={String(ungraded)} />
            <StatTile label="Kept" value={String(kept)} />
            <StatTile label="Reverted" value={String(reverted)} />
          </div>

          <Card>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Change</TableHead>
                  <TableHead>Why</TableHead>
                  <TableHead>Metric</TableHead>
                  <TableHead className="text-right">Baseline</TableHead>
                  <TableHead className="text-right">Observed</TableHead>
                  <TableHead>Status</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {sorted.map((row) => (
                  <TableRow key={row.id}>
                    <TableCell className="align-top">
                      <div className="font-mono text-xs">
                        {row.action} {ARTIFACT[row.artifact] ?? row.artifact}
                      </div>
                      {row.target && (
                        <div className="mt-1 truncate font-mono text-[11px] text-muted-foreground">
                          {row.target}
                        </div>
                      )}
                      {row.issue_url && (
                        <a
                          href={row.issue_url}
                          target="_blank"
                          rel="noreferrer"
                          className="mt-1 inline-block font-mono text-[11px] text-primary hover:underline"
                        >
                          #{row.issue_number ?? "issue"}
                        </a>
                      )}
                    </TableCell>
                    {/* The rationale is the column this page exists for. Six weeks from now
                        the question about any rule is "who decided this and on what", and an
                        unattributable rule is one nobody dares delete. */}
                    <TableCell className="align-top">
                      <p className="max-w-prose text-xs">{row.rationale}</p>
                      {row.evidence?.run_ids?.length ? (
                        <div className="mt-1 font-mono text-[11px] text-muted-foreground">
                          {row.evidence.run_ids.length} run
                          {row.evidence.run_ids.length === 1 ? "" : "s"} cited
                        </div>
                      ) : null}
                    </TableCell>
                    <TableCell className="align-top font-mono text-[11px]">{row.metric}</TableCell>
                    <TableCell className="align-top text-right font-mono text-xs">
                      {num(row.baseline)}
                    </TableCell>
                    <TableCell className="align-top text-right font-mono text-xs">
                      {num(row.observed)}
                      <div className="text-[11px] text-muted-foreground">
                        {moved(row.baseline, row.observed)}
                      </div>
                    </TableCell>
                    <TableCell className="align-top">
                      <StateBadge state={row.status} />
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </Card>

          <p className="mt-4 flex items-center gap-2 font-mono text-[11px] text-muted-foreground">
            <Sprout className="size-3.5" />
            Read-only. A status moves when the factory actually builds or merges the issue —
            never from here.
          </p>
        </>
      )}
    </>
  )
}
