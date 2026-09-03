import { useMemo, useState } from "react"
import { Bug as BugIcon, ExternalLink } from "lucide-react"
import { usePoll, type BugsResponse, type Bug } from "@/lib/api"
import { ago } from "@/lib/format"
import { Card } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Input } from "@/components/ui/input"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { PageHeader, Empty, ErrorNote } from "@/components/Page"
import { StatTile } from "@/components/Meter"

/** Sentry's levels, ranked. The sync mirrors everything and this page filters, so the
 *  ranking lives here — the API refusing rows would make the two disagree about what
 *  exists. */
const LEVELS = ["fatal", "error", "warning", "info", "debug"]

const LEVEL_VARIANT: Record<string, "bad" | "warn" | "muted"> = {
  fatal: "bad",
  error: "bad",
  warning: "warn",
  info: "muted",
  debug: "muted",
}

/** `regressed` is the substatus worth shouting about: the error came back after somebody
 *  resolved it, which is the one state that says a fix did not hold. */
function statusLabel(b: Bug): { text: string; variant: "ok" | "warn" | "bad" | "muted" } {
  if (b.substatus === "regressed") return { text: "regressed", variant: "bad" }
  if (b.status === "resolved") return { text: "resolved", variant: "ok" }
  if (b.status === "ignored") return { text: "ignored", variant: "muted" }
  return { text: b.status || "unresolved", variant: "warn" }
}

/** A filter row's buttons: "all" plus whatever values actually occur in the data. Derived
 *  rather than hardcoded so a value Sentry adds shows up as a button instead of vanishing. */
function Picker({
  options,
  value,
  onPick,
}: {
  options: string[]
  value: string | null
  onPick: (v: string | null) => void
}) {
  return (
    <div className="flex flex-wrap gap-1">
      {[null, ...options].map((opt) => (
        <button
          key={opt ?? "all"}
          onClick={() => onPick(opt)}
          className={`border px-2.5 py-1 font-mono text-xs ${
            opt === value
              ? "border-foreground bg-foreground text-background"
              : "border-border text-muted-foreground hover:text-foreground"
          }`}
        >
          {opt ?? "all"}
        </button>
      ))}
    </div>
  )
}

export function Bugs() {
  const { data, error } = usePoll<BugsResponse>("/api/bugs", 30000)
  const [repo, setRepo] = useState<string | null>(null)
  const [level, setLevel] = useState<string | null>(null)
  const [status, setStatus] = useState<string | null>(null)
  const [query, setQuery] = useState("")

  const bugs = useMemo(() => data?.bugs ?? [], [data])
  const repos = useMemo(() => [...new Set(bugs.map((b) => b.repo))].sort(), [bugs])
  const levels = LEVELS.filter((l) => bugs.some((b) => b.level === l))
  const statuses = [...new Set(bugs.map((b) => statusLabel(b).text))].sort()

  const q = query.trim().toLowerCase()
  const shown = bugs.filter(
    (b) =>
      (!repo || b.repo === repo) &&
      (!level || b.level === level) &&
      (!status || statusLabel(b).text === status) &&
      (!q || `${b.title} ${b.culprit} ${b.short_id}`.toLowerCase().includes(q)),
  )

  const unresolved = bugs.filter((b) => b.status === "unresolved").length
  const regressed = bugs.filter((b) => b.substatus === "regressed").length
  const events = bugs.reduce((n, b) => n + b.count, 0)

  return (
    <>
      <PageHeader
        kicker="Monitoring"
        title="Bugs"
        subtitle={
          <>
            Every production error Sentry has seen in the watched apps, grouped the way
            Sentry groups them — one row per fingerprint, however many times it fired. A
            mirror, refreshed every few minutes; resolving and assigning happen in Sentry.
          </>
        }
      />

      {error && <ErrorNote message={error} />}

      {bugs.length === 0 ? (
        <Empty>
          {data && !data.enabled
            ? "The Sentry sync is off. Set FACTORY_SENTRY=1 (plus the org, token and team) and the factory will create a Sentry project per repo, file the wiring issue, and mirror what it hears."
            : "Nothing synced yet. Either the apps have thrown nothing — enjoy it — or their wiring issues haven't merged."}
        </Empty>
      ) : (
        <>
          <div className="mb-6 grid grid-cols-2 gap-3 sm:grid-cols-4">
            <StatTile label="Bugs" value={String(bugs.length)} />
            <StatTile label="Unresolved" value={String(unresolved)} />
            <StatTile label="Regressed" value={String(regressed)} />
            <StatTile label="Events" value={String(events)} />
          </div>

          <div className="mb-4 flex flex-wrap items-center gap-x-6 gap-y-2">
            {repos.length > 1 && <Picker options={repos} value={repo} onPick={setRepo} />}
            {levels.length > 1 && <Picker options={levels} value={level} onPick={setLevel} />}
            {statuses.length > 1 && <Picker options={statuses} value={status} onPick={setStatus} />}
            <Input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="filter by title or culprit…"
              className="h-7 max-w-56 font-mono text-xs"
            />
          </div>

          {shown.length === 0 ? (
            <Empty>Nothing matches these filters.</Empty>
          ) : (
            <Card>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="w-20">level</TableHead>
                    <TableHead>bug</TableHead>
                    <TableHead className="w-28">status</TableHead>
                    <TableHead className="w-20 text-right">events</TableHead>
                    <TableHead className="w-16 text-right">users</TableHead>
                    <TableHead className="w-28">last seen</TableHead>
                    <TableHead className="w-12" />
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {shown.map((b) => {
                    const s = statusLabel(b)
                    return (
                      <TableRow key={b.id}>
                        <TableCell className="align-top">
                          <Badge variant={LEVEL_VARIANT[b.level] ?? "default"}>
                            {b.level || "—"}
                          </Badge>
                        </TableCell>
                        {/* Sentry-authored strings render as text nodes only — React escapes
                            them, and nothing here ever treats them as markup. */}
                        <TableCell className="max-w-xl align-top">
                          <div className="truncate font-mono text-xs">{b.title}</div>
                          <div className="mt-1 truncate font-mono text-[11px] text-muted-foreground">
                            {b.short_id && <span className="mr-2">{b.short_id}</span>}
                            {b.culprit}
                            {repos.length > 1 && !repo && (
                              <span className="ml-2">· {b.repo}</span>
                            )}
                          </div>
                        </TableCell>
                        <TableCell className="align-top">
                          <Badge variant={s.variant}>{s.text}</Badge>
                        </TableCell>
                        <TableCell className="align-top text-right font-mono text-xs">
                          {b.count}
                        </TableCell>
                        <TableCell className="align-top text-right font-mono text-xs">
                          {b.user_count}
                        </TableCell>
                        <TableCell className="align-top font-mono text-[11px] text-muted-foreground">
                          {ago(b.last_seen)}
                        </TableCell>
                        <TableCell className="align-top">
                          {b.permalink && (
                            <a
                              href={b.permalink}
                              target="_blank"
                              rel="noreferrer"
                              className="text-muted-foreground hover:text-foreground"
                              title="open in Sentry"
                            >
                              <ExternalLink className="size-3.5" />
                            </a>
                          )}
                        </TableCell>
                      </TableRow>
                    )
                  })}
                </TableBody>
              </Table>
            </Card>
          )}

          <p className="mt-4 flex items-center gap-2 font-mono text-[11px] text-muted-foreground">
            <BugIcon className="size-3.5" />
            Read-only. Sentry is the source of truth; this list is what the last sync heard.
            Deciding what to fix stays with you — for now.
          </p>
        </>
      )}
    </>
  )
}
