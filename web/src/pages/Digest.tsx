import { useState } from "react"
import { usePoll, type Digest as DigestData, type DigestCluster } from "@/lib/api"
import { Card } from "@/components/ui/card"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { PageHeader, SectionHead, Empty, ErrorNote } from "@/components/Page"
import { StatTile } from "@/components/Meter"

/** How far back to look. Wider than the learning trigger on purpose — a change is judged
 *  against what happened before it, so the window has to hold both sides. */
const WINDOWS = [7, 14, 30, 90]

function usd(n: number): string {
  return `$${n.toFixed(2)}`
}

/** A cluster of failures that share a signature.
 *
 *  The run ids are shown rather than summarised because they are the point: a proposal built
 *  on this digest has to cite them, and a cluster whose evidence you cannot open is one you
 *  cannot check the loop's reasoning against. */
function Clusters({ rows, empty }: { rows: DigestCluster[]; empty: string }) {
  if (!rows.length) return <Empty>{empty}</Empty>
  return (
    <Card>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>what happened</TableHead>
            <TableHead className="w-16 text-right">times</TableHead>
            <TableHead className="w-44">issues</TableHead>
            <TableHead className="w-56">evidence</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((c) => (
            <TableRow key={c.signature}>
              <TableCell className="max-w-xl">
                <div className="truncate font-mono text-xs">{c.example}</div>
                {c.repos.length > 1 && (
                  <div className="mt-1 text-xs text-muted-foreground">
                    across {c.repos.join(", ")}
                  </div>
                )}
              </TableCell>
              <TableCell className="text-right font-mono text-xs">{c.count}</TableCell>
              <TableCell className="font-mono text-xs text-muted-foreground">
                {c.issues.length ? c.issues.map((n) => `#${n}`).join(" ") : "—"}
              </TableCell>
              <TableCell className="font-mono text-[11px] text-muted-foreground">
                {c.run_ids.map((r) => (
                  <a key={r} href={`/runs/${r}`} className="mr-2 underline-offset-2 hover:underline">
                    {r.slice(0, 8)}
                  </a>
                ))}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </Card>
  )
}

export function Digest() {
  const [days, setDays] = useState(30)
  const { data, error } = usePoll<DigestData>(`/api/digest?days=${days}`, 60000)

  const rejections = data?.rejections ?? []
  const failures = data?.failures ?? []
  const outcomes = data?.outcomes ?? []
  const truncated = Object.entries(data?.truncated ?? {})

  // Fleet-wide totals, in the order the loop's objective ranks them: what got sent back,
  // what shipped nothing, and only then what it cost.
  const issues = outcomes.reduce((n, o) => n + o.issues, 0)
  const shipped = outcomes.reduce((n, o) => n + o.shipped, 0)
  const wrong = outcomes.reduce((n, o) => n + o.failed_runs + o.shipped_nothing, 0)
  const retries = outcomes.reduce((n, o) => n + o.retries, 0)
  const spend = (data?.cost ?? []).reduce((n, c) => n + c.spend, 0)
  const wasted = (data?.cost ?? []).reduce((n, c) => n + c.wasted, 0)

  return (
    <div>
      <PageHeader
        kicker="Evidence"
        title="Digest"
        subtitle={
          <>
            What the last {days} days actually cost in rejections and failures — the same
            document a learning run is handed. Read it before trusting what the loop proposes
            from it: a proposal built on a bad digest is wrong <em>and</em> plausible.
          </>
        }
        actions={
          <div className="flex gap-1">
            {WINDOWS.map((d) => (
              <button
                key={d}
                onClick={() => setDays(d)}
                className={`border px-2.5 py-1 font-mono text-xs ${
                  d === days
                    ? "border-foreground bg-foreground text-background"
                    : "border-border text-muted-foreground hover:text-foreground"
                }`}
              >
                {d}d
              </button>
            ))}
          </div>
        }
      />

      {error && <ErrorNote message={error} />}

      <div className="mb-8 grid grid-cols-2 gap-4 lg:grid-cols-4">
        <StatTile
          label="sent back"
          value={String(rejections.reduce((n, r) => n + r.count, 0))}
          note="reviews that rejected"
          featured
        />
        <StatTile label="shipped nothing" value={String(wrong)} note={`${retries} retries`} />
        <StatTile label="issues shipped" value={`${shipped}/${issues}`} note="reached a PR" />
        <StatTile label="spend" value={usd(spend)} note={`${usd(wasted)} bought nothing`} />
      </div>

      {/* Named explicitly, because a cap that drops rows silently reads as "this is
          everything" — and an agent asked to propose from a complete picture will do
          exactly that with a partial one. */}
      {truncated.length > 0 && (
        <div className="mb-6 border border-border bg-card/50 px-4 py-2.5 font-mono text-xs text-muted-foreground">
          truncated: {truncated.map(([k, n]) => `${k} (+${n} more)`).join(" · ")}
        </div>
      )}

      <div className="mb-8">
        <SectionHead>Why reviews sent work back</SectionHead>
        <Clusters
          rows={rejections}
          empty="No review rejected anything in this window."
        />
      </div>

      <div className="mb-8">
        <SectionHead>How runs failed</SectionHead>
        <Clusters rows={failures} empty="No run failed in this window." />
      </div>

      <div className="mb-8">
        <SectionHead>What memory did about it</SectionHead>
        {/* The comparison is the whole value. "Runs primed memory" says nothing alone;
            runs that went wrong opening a fraction of what the ones that shipped opened is
            a lead worth chasing. Still only a lead — this cannot see whether an opened
            record was relevant, and a run that opened nothing may have needed nothing. */}
        {(data?.retrieval ?? []).length ? (
          <Card>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>repo</TableHead>
                  <TableHead>outcome</TableHead>
                  <TableHead className="text-right">runs</TableHead>
                  <TableHead className="text-right">primed</TableHead>
                  <TableHead className="text-right">records opened</TableHead>
                  <TableHead className="text-right">index size</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {(data?.retrieval ?? []).map((r) => (
                  <TableRow key={`${r.repo}-${r.outcome}`}>
                    <TableCell className="font-mono text-xs">{r.repo}</TableCell>
                    <TableCell className="font-mono text-xs">
                      {r.outcome === "went_wrong" ? "went wrong" : "went fine"}
                    </TableCell>
                    <TableCell className="text-right font-mono text-xs">{r.runs}</TableCell>
                    <TableCell className="text-right font-mono text-xs">{r.primed}</TableCell>
                    <TableCell className="text-right font-mono text-xs">{r.records_opened}</TableCell>
                    <TableCell className="text-right font-mono text-xs">
                      {Math.round(r.avg_index_size)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </Card>
        ) : (
          <Empty>No run reported on memory in this window.</Empty>
        )}
      </div>

      <div className="mb-8">
        <SectionHead>Skills that were loaded</SectionHead>
        {/* An absence here is the eviction signal — but only against the skills a repo
            actually has, which this cannot see. A skill missing from both lists is one
            nobody wrote; a skill in the repo and missing here is either unused or shadowed
            by a global skill of the same name. */}
        {(data?.skills ?? []).length ? (
          <Card>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>repo</TableHead>
                  <TableHead>skill</TableHead>
                  <TableHead className="text-right">loads</TableHead>
                  <TableHead className="text-right">runs</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {(data?.skills ?? []).map((s) => (
                  <TableRow key={`${s.repo}-${s.skill}`}>
                    <TableCell className="font-mono text-xs">{s.repo}</TableCell>
                    <TableCell className="font-mono text-xs">{s.skill}</TableCell>
                    <TableCell className="text-right font-mono text-xs">{s.loads}</TableCell>
                    <TableCell className="text-right font-mono text-xs">{s.runs}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </Card>
        ) : (
          <Empty>No run loaded a skill in this window.</Empty>
        )}
      </div>

      <div>
        <SectionHead>Tools that failed</SectionHead>
        {(data?.tool_errors ?? []).length ? (
          <Card>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-40">tool</TableHead>
                  <TableHead className="w-20 text-right">failures</TableHead>
                  <TableHead className="w-20 text-right">runs</TableHead>
                  <TableHead>example</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {(data?.tool_errors ?? []).map((t) => (
                  <TableRow key={t.tool}>
                    <TableCell className="font-mono text-xs">{t.tool}</TableCell>
                    <TableCell className="text-right font-mono text-xs">{t.failures}</TableCell>
                    <TableCell className="text-right font-mono text-xs">{t.runs}</TableCell>
                    <TableCell className="max-w-lg truncate font-mono text-[11px] text-muted-foreground">
                      {t.example ?? "—"}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </Card>
        ) : (
          <Empty>No tool call failed in this window.</Empty>
        )}
      </div>
    </div>
  )
}
