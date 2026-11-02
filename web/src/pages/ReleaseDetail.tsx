import { useState, type FormEvent, type ReactNode } from "react"
import { Link, useParams } from "react-router-dom"
import { ArrowLeft } from "lucide-react"
import {
  addFeedback,
  fileFeedback,
  markReleaseReviewed,
  usePoll,
  type Feedback,
  type Packet,
  type PacketItem,
} from "@/lib/api"
import { ago, cost, shortId } from "@/lib/format"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { PageHeader, SectionHead, ErrorNote, Empty } from "@/components/Page"
import { StateBadge } from "@/components/StateBadge"
import { Badge } from "@/components/ui/badge"

const STANDING = ["promoted", "deployed", "accepted", "rejected"]

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="border-l border-subtle pl-3">
      <div className="eyebrow text-muted-foreground">{label}</div>
      <div className="mt-1.5 font-mono text-sm">{children}</div>
    </div>
  )
}

function sha(value: string | null | undefined): string {
  return value ? value.slice(0, 8) : "—"
}

/** A criterion is only "met" when the reviewer said so with evidence. Everything else —
 *  including a criterion it never mentioned — is shown as the gap it is. The same reading
 *  serves the browser's per-issue statuses. */
function verdictVariant(status: string): "ok" | "warn" | "bad" | "muted" {
  if (status === "met" || status === "pass") return "ok"
  if (status === "not_applicable") return "muted"
  if (status === "not reported" || status === "cannot_verify") return "warn"
  return "bad"
}

function ItemRow({ item, acceptance }: { item: PacketItem; acceptance?: { status: string; evidence: string } }) {
  const unmet = item.criteria.filter((c) => c.status !== "met").length
  return (
    <TableRow>
      <TableCell className="align-top font-mono">
        #{item.issue_number}
        {item.pr_url && (
          <a href={item.pr_url} target="_blank" rel="noreferrer" className="mt-1 block text-[11px] text-primary hover:underline">
            pull request
          </a>
        )}
      </TableCell>
      <TableCell className="align-top">
        <div className="text-sm">{item.issue_title}</div>
        {item.summary && <p className="mt-1 max-w-prose text-xs text-muted-foreground">{item.summary}</p>}
        <details className="mt-2">
          <summary className="cursor-pointer font-mono text-[11px] text-muted-foreground">
            {item.criteria.length} criteria{unmet ? `, ${unmet} not met or not reported` : ", all met"}
            {item.review ? ` — review ${shortId(item.review.run_id)}` : " — no review on record"}
          </summary>
          <ul className="mt-2 space-y-2">
            {item.criteria.map((c) => (
              <li key={c.id} className="border-l border-subtle pl-3">
                <div className="flex items-center gap-2 font-mono text-[11px]">
                  <Badge variant={verdictVariant(c.status)}>{c.status}</Badge>
                  <span>{c.id}</span>
                  {c.mode && <span className="text-muted-foreground">{c.mode}</span>}
                </div>
                {c.statement && <div className="mt-1 text-xs">{c.statement}</div>}
                {c.evidence && (
                  <pre className="mt-1 max-w-prose whitespace-pre-wrap font-mono text-[11px] text-muted-foreground">
                    {c.evidence}
                  </pre>
                )}
              </li>
            ))}
          </ul>
          {item.review?.notes.length ? (
            <ul className="mt-2 list-disc pl-5 text-[11px] text-muted-foreground">
              {item.review.notes.map((n) => (
                <li key={n}>{n}</li>
              ))}
            </ul>
          ) : null}
        </details>
      </TableCell>
      <TableCell className="align-top">
        {acceptance ? (
          <>
            <Badge variant={verdictVariant(acceptance.status)}>{acceptance.status}</Badge>
            <p className="mt-1 max-w-xs text-[11px] text-muted-foreground">{acceptance.evidence}</p>
          </>
        ) : (
          <span className="font-mono text-[11px] text-muted-foreground">—</span>
        )}
      </TableCell>
    </TableRow>
  )
}

function FeedbackRow({ releaseId, note, onDone }: { releaseId: number; note: Feedback; onDone: () => void }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  return (
    <li className="border-l border-subtle py-1 pl-3">
      <pre className="whitespace-pre-wrap font-sans text-sm">{note.text}</pre>
      <div className="mt-1 flex flex-wrap items-center gap-3 font-mono text-[11px] text-muted-foreground">
        <span>{ago(note.created_at)}</span>
        {note.issue_url ? (
          <a href={note.issue_url} target="_blank" rel="noreferrer" className="text-primary hover:underline">
            filed as #{note.issue_number}
          </a>
        ) : (
          <>
            {note.gate_reason && (
              <span className="text-warn" title="Filed as a plain issue rather than agent:queued: the poller would block it, and a blocked issue halts the repo's queue">
                filed for a human, not queued: {note.gate_reason}
              </span>
            )}
            <Button
              variant="ghost"
              size="sm"
              disabled={busy}
              title={note.gate_reason ? "Open this note as a plain issue on the repo" : "Open this note as an agent:queued issue the factory will build"}
              onClick={async () => {
                setBusy(true)
                setError(null)
                try {
                  await fileFeedback(releaseId, note.id)
                  onDone()
                } catch (e) {
                  setError(e instanceof Error ? e.message : String(e))
                } finally {
                  setBusy(false)
                }
              }}
            >
              {busy ? "Filing…" : "File as issue"}
            </Button>
          </>
        )}
        {error && <span className="text-bad">{error}</span>}
      </div>
    </li>
  )
}

function FeedbackForm({ releaseId, onDone }: { releaseId: number; onDone: () => void }) {
  const [text, setText] = useState("")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await addFeedback(releaseId, text)
      setText("")
      onDone()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form onSubmit={onSubmit} className="mt-4">
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        rows={5}
        placeholder={"What you saw on the test server. To have the factory build a fix, end with an\n## Acceptance criteria block (factory-compose format)."}
        className="w-full border border-input bg-background px-3 py-2 font-mono text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
        disabled={busy}
      />
      <div className="mt-2 flex items-center gap-3">
        <Button type="submit" variant="outline" size="sm" disabled={busy || !text.trim()}>
          {busy ? "Saving…" : "Save note"}
        </Button>
        <span className="font-mono text-[11px] text-muted-foreground">
          Saved notes are not filed until you say so.
        </span>
        {error && <span className="font-mono text-[11px] text-bad">{error}</span>}
      </div>
    </form>
  )
}

export function ReleaseDetail() {
  const { releaseId = "" } = useParams()
  const id = Number(releaseId)
  const { data, error, refresh } = usePoll<Packet>(`/api/releases/${id}/packet`, 15000)
  const [reviewError, setReviewError] = useState<string | null>(null)
  const r = data?.release

  const accepted = new Map(
    (data?.acceptance?.verdict.items ?? []).map((i) => [i.issue, { status: i.status, evidence: i.evidence }])
  )

  return (
    <div>
      <PageHeader
        kicker={`Release #${id}`}
        title={r ? r.repo : "Release"}
        subtitle={r ? `${sha(r.base_sha_before)} → ${sha(r.test_sha)}` : undefined}
        actions={
          <>
            <Button asChild variant="ghost">
              <Link to="/releases">
                <ArrowLeft /> Back
              </Link>
            </Button>
            {r && STANDING.includes(r.status) && (
              <Button
                variant="outline"
                onClick={async () => {
                  setReviewError(null)
                  try {
                    await markReleaseReviewed(r.id)
                    refresh()
                  } catch (e) {
                    setReviewError(e instanceof Error ? e.message : String(e))
                  }
                }}
              >
                Mark reviewed
              </Button>
            )}
          </>
        }
      />

      {error && <ErrorNote message={error} />}
      {reviewError && <ErrorNote message={reviewError} />}

      {r && data && (
        <>
          <Card className="mb-6 p-optical-lg">
            <div className="grid grid-cols-2 gap-x-6 gap-y-6 sm:grid-cols-4">
              <Field label="Status">
                <StateBadge state={r.status} />
              </Field>
              <Field label="Test server">
                {r.test_url ? (
                  <a href={r.test_url} target="_blank" rel="noreferrer" className="text-primary underline-offset-4 hover:underline">
                    {r.test_url}
                  </a>
                ) : (
                  "—"
                )}
              </Field>
              <Field label="Deployed">{r.deployed_sha ? `${sha(r.deployed_sha)} ${ago(r.deployed_at)}` : "—"}</Field>
              <Field label="Promoted">{ago(r.promoted_at)}</Field>
            </div>
            {r.error && <p className="mt-5 font-mono text-xs text-muted-foreground">{r.error}</p>}
          </Card>

          <SectionHead>What it carried</SectionHead>
          {data.items.length === 0 ? (
            <Empty>No factory-built changes recorded for this release.</Empty>
          ) : (
            <Card className="mb-6">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Issue</TableHead>
                    <TableHead>Change and how it was verified</TableHead>
                    <TableHead>In the browser</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data.items.map((item) => (
                    <ItemRow key={item.merged_sha} item={item} acceptance={accepted.get(item.issue_number)} />
                  ))}
                </TableBody>
              </Table>
            </Card>
          )}

          <SectionHead>On the test server</SectionHead>
          <Card className="mb-6 p-optical">
            <div className="space-y-2 font-mono text-xs">
              <div>
                Deploy:{" "}
                {data.deploy ? (
                  <Link to={`/runs/${data.deploy.id}`} className="text-primary hover:underline">
                    {data.deploy.status} on {data.deploy.vm_name}
                  </Link>
                ) : (
                  "not deployed"
                )}
                {data.deploy?.error && <span className="text-muted-foreground"> — {data.deploy.error}</span>}
              </div>
              <div>
                Acceptance:{" "}
                {data.acceptance ? (
                  <>
                    <Link to={`/runs/${data.acceptance.id}`} className="text-primary hover:underline">
                      {data.acceptance.verdict.verdict ?? data.acceptance.status}
                    </Link>{" "}
                    <span className="text-muted-foreground">{cost(data.acceptance.cost_usd)}</span>
                    {data.acceptance.verdict.findings?.length ? (
                      <ul className="mt-1 list-disc pl-5 text-muted-foreground">
                        {data.acceptance.verdict.findings.map((f) => (
                          <li key={f}>{f}</li>
                        ))}
                      </ul>
                    ) : null}
                  </>
                ) : (
                  "no browser acceptance run"
                )}
              </div>
              {r.repair_issue && <div>Repair issue: #{r.repair_issue}</div>}
              {r.status === "rejected" && (
                <p className="text-muted-foreground">
                  A rejected release is not rolled back automatically: the test server keeps it
                  until the repair merges and the next release deploys, or a human restores it.
                </p>
              )}
            </div>
          </Card>

          <SectionHead>Feedback</SectionHead>
          <Card className="p-optical">
            {data.feedback.length === 0 ? (
              <p className="font-mono text-xs text-muted-foreground">No notes yet.</p>
            ) : (
              <ul className="space-y-3">
                {data.feedback.map((f) => (
                  <FeedbackRow key={f.id} releaseId={id} note={f} onDone={refresh} />
                ))}
              </ul>
            )}
            <FeedbackForm releaseId={id} onDone={refresh} />
          </Card>
        </>
      )}
    </div>
  )
}
