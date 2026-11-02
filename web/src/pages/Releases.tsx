import { useState } from "react"
import { Rocket } from "lucide-react"
import {
  assembleRelease,
  deployRelease,
  markReleaseReviewed,
  usePoll,
  type Project,
  type Release,
} from "@/lib/api"
import { ago } from "@/lib/format"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { PageHeader, Empty, ErrorNote } from "@/components/Page"
import { StateBadge } from "@/components/StateBadge"

/** Statuses in which a release is standing on the test server waiting for a human. These are
 *  the ones "Mark reviewed" applies to, and the ones that hold the repo frozen. */
const STANDING: Release["status"][] = ["promoted", "deployed", "accepted", "rejected"]

function sha(value: string | null): string {
  return value ? value.slice(0, 8) : "—"
}

/** One button per watched repo: assemble a release now rather than at the next moment. */
function AssembleBar({ onDone }: { onDone: () => void }) {
  const { data: projects } = usePoll<Project[]>("/api/projects")
  const [busy, setBusy] = useState<string | null>(null)
  const [note, setNote] = useState<string | null>(null)

  async function assemble(repo: string) {
    setBusy(repo)
    setNote(null)
    try {
      const { release, reason } = await assembleRelease(repo)
      setNote(release ? `${repo}: release ${release.id} ${reason}` : `${repo}: ${reason}`)
      onDone()
    } catch (e) {
      setNote(`${repo}: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setBusy(null)
    }
  }

  if (!projects?.length) return null
  return (
    <div className="mb-5 flex flex-wrap items-center gap-2">
      {projects.map((p) => (
        <Button
          key={p.repo}
          variant="outline"
          size="sm"
          disabled={busy !== null}
          title={`Promote ${p.base_branch ?? "the default branch"} onto the release branch now, once CI is green`}
          onClick={() => assemble(p.repo)}
        >
          <Rocket /> {busy === p.repo ? "Assembling…" : `Release ${p.repo}`}
        </Button>
      ))}
      {note && <span className="font-mono text-[11px] text-muted-foreground">{note}</span>}
    </div>
  )
}

function Actions({ release, onDone }: { release: Release; onDone: () => void }) {
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function act(what: string, work: () => Promise<unknown>) {
    setBusy(what)
    setError(null)
    try {
      await work()
      onDone()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="flex items-center justify-end gap-2">
      {error && <span className="font-mono text-[10px] text-bad">{error}</span>}
      {release.status === "promoted" && (
        <Button
          variant="ghost"
          size="sm"
          disabled={busy !== null}
          title="Deploy this release to the repo's test server now, as its ## Deploy section says"
          onClick={() => act("deploy", () => deployRelease(release.id))}
        >
          {busy === "deploy" ? "Starting…" : "Deploy"}
        </Button>
      )}
      {STANDING.includes(release.status) && (
        <Button
          variant="ghost"
          size="sm"
          disabled={busy !== null}
          title="You have read this release. Lifts the freeze so the next moment can release again."
          onClick={() => act("reviewed", () => markReleaseReviewed(release.id))}
        >
          {busy === "reviewed" ? "Saving…" : "Mark reviewed"}
        </Button>
      )}
    </div>
  )
}

export function Releases() {
  const { data, error, refresh } = usePoll<Release[]>("/api/releases", 15000)
  const rows = data ?? []

  return (
    <>
      <PageHeader
        kicker="Release pipeline"
        title="Releases"
        subtitle={
          <>
            Verified integration heads promoted onto the release branch at fixed moments of the
            day. A release that is not yet reviewed holds its repo frozen, so the test server
            stands still while somebody looks at it.
          </>
        }
      />
      <AssembleBar onDone={refresh} />
      {error && <ErrorNote message={error} />}
      {rows.length === 0 ? (
        <Empty>
          No releases yet. They are assembled at FACTORY_RELEASE_TIMES when FACTORY_RELEASE=1, or
          by hand with the buttons above.
        </Empty>
      ) : (
        <Card>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Release</TableHead>
                <TableHead>Repo</TableHead>
                <TableHead>Commit</TableHead>
                <TableHead className="text-right">Items</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>When</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((r) => (
                <TableRow key={r.id}>
                  <TableCell className="font-mono">#{r.id}</TableCell>
                  <TableCell className="whitespace-nowrap font-mono">{r.repo}</TableCell>
                  <TableCell className="font-mono text-xs">
                    {sha(r.base_sha_before)} → {sha(r.test_sha)}
                    {r.test_url && (
                      <a
                        href={r.test_url}
                        target="_blank"
                        rel="noreferrer"
                        className="mt-1 block text-primary underline-offset-4 hover:underline"
                      >
                        {r.test_url}
                      </a>
                    )}
                  </TableCell>
                  <TableCell className="text-right font-mono tabular-nums">{r.item_count ?? 0}</TableCell>
                  <TableCell>
                    <StateBadge state={r.status} />
                    {r.error && (
                      <div className="mt-1 max-w-md font-mono text-[11px] text-muted-foreground">
                        {r.error}
                      </div>
                    )}
                  </TableCell>
                  <TableCell className="whitespace-nowrap font-mono text-xs text-muted-foreground">
                    {ago(r.promoted_at ?? r.created_at)}
                  </TableCell>
                  <TableCell>
                    <Actions release={r} onDone={refresh} />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Card>
      )}
    </>
  )
}
