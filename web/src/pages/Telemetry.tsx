import { BarChart3 } from "lucide-react"
import { usePoll, type Telemetry as TelemetryData } from "@/lib/api"
import { cost } from "@/lib/format"
import { Card } from "@/components/ui/card"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { PageHeader, Empty, ErrorNote } from "@/components/Page"
import { Meter, StatTile } from "@/components/Meter"

/** Panel masthead: serif title over a hairline, the same voice as the page header one
 *  level down. The standfirst stays in Inter — it is prose, not a label. */
function PanelHead({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="border-b border-subtle p-optical pb-3">
      <h2 className="font-serif text-2xl leading-none">{title}</h2>
      <p className="mt-2 max-w-prose text-xs text-muted-foreground">{children}</p>
    </div>
  )
}

function pct(part: number, whole: number): string {
  if (!whole) return "—"
  return `${((part / whole) * 100).toFixed(1)}%`
}

function minutes(ms: number): string {
  if (ms >= 3_600_000) return `${(ms / 3_600_000).toFixed(1)}h`
  if (ms >= 60_000) return `${Math.round(ms / 60_000)}m`
  return `${Math.round(ms / 1000)}s`
}

export function Telemetry() {
  const { data, error } = usePoll<TelemetryData>("/api/telemetry", 30000)

  const composition = data?.composition
  // Named in the order they appear on the bill, not alphabetically: the reader is
  // scanning for which class dominates, so the largest belongs first.
  const classes = composition
    ? ([
        ["Cache read", composition.cache_read],
        ["Cache write", composition.cache_write],
        ["Output", composition.output],
        ["Input", composition.input],
      ] as [string, number][])
    : []
  const total = classes.reduce((sum, [, v]) => sum + v, 0)

  const economics = data?.economics ?? []
  const spend = economics.reduce((s, e) => s + e.spend, 0)
  const wasted = economics.reduce((s, e) => s + e.wasted, 0)
  const shipped = economics.reduce((s, e) => s + e.shipped, 0)
  const runs = economics.reduce((s, e) => s + e.runs, 0)

  const tools = data?.tools ?? []
  const slowestTool = tools[0]?.duration_ms ?? 0
  const days = data?.spend_by_day ?? []
  const busiestDay = days.reduce((m, d) => Math.max(m, d.derived_cost_usd), 0)

  const empty = data && runs === 0 && total === 0

  return (
    <div>
      <PageHeader
        kicker="The bill"
        title="Telemetry"
        subtitle="Every model call and tool call the factory has made, priced against model_prices."
      />
      {error && <ErrorNote message={error} />}

      {empty && (
        <Empty>
          <BarChart3 className="mx-auto mb-3 size-6" />
          No calls recorded yet. Rows land as runs happen.
          <div className="mt-2 text-xs">
            To load history from runs that finished earlier:{" "}
            <code className="border border-subtle bg-muted px-1.5 py-0.5 font-mono">
              python -m telemetry.backfill
            </code>
          </div>
        </Empty>
      )}

      {data && !empty && (
        <div className="space-y-6">
          {/* Asymmetric on purpose. Cost per shipped issue is the figure the run table
              could never produce — an issue costs its build plus every retry, review and
              fix, and only the ones that opened a PR count as delivered — so it takes
              half the row and the accent shadow, and the other three report to it. */}
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-6">
            <StatTile
              featured
              className="sm:col-span-2 lg:col-span-3"
              label="Cost per shipped issue"
              value={shipped ? cost(spend / shipped) : "—"}
              note={shipped ? `${shipped} issues reached a pull request` : "nothing shipped yet"}
            />
            <StatTile label="Total spend" value={cost(spend)} note={`${runs} runs observed`} />
            <StatTile
              label="Wasted spend"
              value={cost(wasted)}
              note={spend ? `${pct(wasted, spend)} of everything spent` : undefined}
            />
            <StatTile
              label="Tool wall time"
              value={minutes(tools.reduce((s, t) => s + t.duration_ms, 0))}
              note={`across ${tools.reduce((s, t) => s + t.calls, 0)} calls`}
            />
          </div>

          <Card>
            <PanelHead title="Where the money goes">
              By token class. Cache reads are the cost of rediscovery — the agent
              re-reading a codebase it has already read.
            </PanelHead>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Token class</TableHead>
                  <TableHead className="w-1/2">Share</TableHead>
                  <TableHead className="text-right">Cost</TableHead>
                  <TableHead className="text-right">%</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {classes.map(([label, value]) => (
                  <TableRow key={label}>
                    <TableCell>{label}</TableCell>
                    <TableCell>
                      <Meter value={value} max={total} />
                    </TableCell>
                    <TableCell className="text-right font-mono tabular-nums">
                      {cost(value)}
                    </TableCell>
                    <TableCell className="text-right font-mono tabular-nums text-muted-foreground">
                      {pct(value, total)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </Card>

          <div className="grid gap-6 lg:grid-cols-5">
            <Card className="lg:col-span-3">
              <PanelHead title="Where the time goes">
                Tools by total wall time. A tool with failures is a harness problem, not
                an agent one.
              </PanelHead>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Tool</TableHead>
                    <TableHead className="w-1/3">Time</TableHead>
                    <TableHead className="text-right">Calls</TableHead>
                    <TableHead className="text-right">Failed</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {tools.map((t) => (
                    <TableRow key={t.tool}>
                      <TableCell className="font-mono">{t.tool}</TableCell>
                      <TableCell>
                        <div className="flex items-center gap-2">
                          <Meter value={t.duration_ms} max={slowestTool} />
                          <span className="shrink-0 font-mono text-xs tabular-nums text-muted-foreground">
                            {minutes(t.duration_ms)}
                          </span>
                        </div>
                      </TableCell>
                      <TableCell className="text-right font-mono tabular-nums">
                        {t.calls}
                      </TableCell>
                      <TableCell className="text-right font-mono tabular-nums">
                        {t.failures ? <span className="text-bad">{t.failures}</span> : "—"}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </Card>

            <Card className="lg:col-span-2">
              <PanelHead title="Spend by day">
                Derived from the price table, not from what the runtime reported.
              </PanelHead>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Day</TableHead>
                    <TableHead className="w-1/2">Spend</TableHead>
                    <TableHead className="text-right">Runs</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {days.map((d) => (
                    <TableRow key={d.day}>
                      <TableCell className="font-mono">{d.day}</TableCell>
                      <TableCell>
                        <div className="flex items-center gap-2">
                          <Meter value={d.derived_cost_usd} max={busiestDay} />
                          <span className="shrink-0 font-mono text-xs tabular-nums text-muted-foreground">
                            {cost(d.derived_cost_usd)}
                          </span>
                        </div>
                      </TableCell>
                      <TableCell className="text-right font-mono tabular-nums">
                        {d.runs}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </Card>
          </div>

          <Card>
            <PanelHead title="Unit economics">
              What a repo costs, and how much of that reached a pull request.
            </PanelHead>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Repo</TableHead>
                  <TableHead className="text-right">Issues</TableHead>
                  <TableHead className="text-right">Runs</TableHead>
                  <TableHead className="text-right">Shipped</TableHead>
                  <TableHead className="text-right">Spend</TableHead>
                  <TableHead className="text-right">Per shipped</TableHead>
                  <TableHead className="text-right">Wasted</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {economics.map((e) => (
                  <TableRow key={e.repo}>
                    <TableCell className="font-mono">{e.repo}</TableCell>
                    <TableCell className="text-right font-mono tabular-nums">
                      {e.issues}
                    </TableCell>
                    <TableCell className="text-right font-mono tabular-nums">{e.runs}</TableCell>
                    <TableCell className="text-right font-mono tabular-nums text-ok">
                      {e.shipped}
                    </TableCell>
                    <TableCell className="text-right font-mono tabular-nums">
                      {cost(e.spend)}
                    </TableCell>
                    <TableCell className="text-right font-mono tabular-nums">
                      {e.shipped ? cost(e.spend / e.shipped) : "—"}
                    </TableCell>
                    <TableCell className="text-right font-mono tabular-nums">
                      {e.wasted > 0 ? (
                        <span className="text-warn">{cost(e.wasted)}</span>
                      ) : (
                        "—"
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </Card>
        </div>
      )}
    </div>
  )
}
