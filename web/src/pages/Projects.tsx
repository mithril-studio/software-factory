import { useState, type FormEvent } from "react"
import { Link } from "react-router-dom"
import { Flame, Plus, Trash2 } from "lucide-react"
import {
  connectRepo,
  disconnectRepo,
  dropGolden,
  preflight,
  replan,
  setGoal,
  usePoll,
  warmGolden,
  type Connected,
  type Preflight,
  type Project,
  type Run,
} from "@/lib/api"
import { CheckList } from "@/components/CheckList"
import { RepoPicker } from "@/components/RepoPicker"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { PageHeader, Empty, ErrorNote } from "@/components/Page"

function gitUrl(repo: string): string {
  return `https://github.com/${repo}`
}

/** Watch a provisioning run to the end, in the panel that started it.
 *
 *  Without this the only thing that moved was the 15-second projects poll, so the line that
 *  says "warming its golden" sat there unchanged for the whole install — indistinguishable
 *  from a warm-up that had died. Polling stops as soon as the run reaches a terminal state. */
function Warming({ runId }: { runId: string }) {
  const { data: run } = usePoll<Run>(`/api/runs/${runId}`, 5000)
  const status = run?.status ?? "queued"
  if (status === "succeeded") {
    return (
      <p className="mt-1 text-muted-foreground">
        Its golden is warm. New runs boot it instead of{" "}
        <span className="font-mono">golden-copy</span>.
      </p>
    )
  }
  if (status === "failed" || status === "cancelled") {
    return (
      <p className="mt-1 text-muted-foreground">
        The warm-up {status} — <Link to={`/runs/${runId}`} className="text-primary underline-offset-4 hover:underline">see why</Link>.
        Its runs clone and install for themselves until it is rebuilt, which costs minutes
        rather than correctness.
      </p>
    )
  }
  return (
    <p className="mt-1 text-muted-foreground">
      Warming its golden ({status}) —{" "}
      <Link to={`/runs/${runId}`} className="text-primary underline-offset-4 hover:underline">
        watch the log
      </Link>
      . Its runs work on <span className="font-mono">golden-copy</span> until that finishes.
    </p>
  )
}

/** Connect a repo: pick it, see what preflight says, decide.
 *
 *  Two steps rather than one because the checks are the point. `POST /api/repos` runs preflight
 *  itself and refuses a repo the token cannot push to — but a refusal arriving as a single line
 *  of red after you pressed Connect tells you less than the same checks did before you pressed
 *  it, and warnings (no `.factory.md`, no warm golden yet) never appear at all if the only time
 *  you see checks is when something blocked.
 *
 *  Connecting is deliberately not gated on provisioning finishing. The repo is dispatchable the
 *  moment it is watched — it boots `golden-copy` and installs for itself — so the warm-up is
 *  reported as a run to go and watch, not as a step this form waits out. */
function Connect({ onDone, onClose }: { onDone: () => void; onClose: () => void }) {
  const [repo, setRepo] = useState("")
  const [checked, setChecked] = useState<Preflight | null>(null)
  const [result, setResult] = useState<Connected | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function run<T>(work: () => Promise<T>, then: (value: T) => void) {
    setBusy(true)
    setError(null)
    try {
      then(await work())
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  function onCheck(e?: FormEvent) {
    e?.preventDefault()
    setResult(null)
    run(() => preflight(repo.trim()), setChecked)
  }

  function onConnect() {
    run(() => connectRepo(repo.trim()), (r) => {
      setResult(r)
      setChecked(null)
      // Clear the field rather than closing the panel. The result below is the only place the
      // warm-up is reported live, so closing would take it away at the moment it starts — but
      // leaving the slug in a field whose Check button still works meant the obvious second
      // press answered "already watched", which reads as a failure.
      setRepo("")
      onDone()
    })
  }

  return (
    <Card className="mb-6 p-optical-lg">
      <form onSubmit={onCheck} className="flex items-end gap-3">
        <RepoPicker
          value={repo}
          disabled={busy}
          onSubmit={() => onCheck()}
          onChange={(next) => {
            setRepo(next)
            setChecked(null)
            setResult(null)
          }}
        />
        <Button type="submit" variant="outline" disabled={busy || !repo.trim()}>
          {busy && !checked ? "Checking…" : "Check"}
        </Button>
      </form>

      {error && (
        <div className="mt-4">
          <ErrorNote message={error} />
        </div>
      )}

      {checked && (
        <div className="mt-5 flex flex-col gap-4">
          <CheckList checks={checked.checks} />
          <div className="flex items-center gap-3">
            <Button onClick={onConnect} disabled={busy || !checked.ready}>
              {busy ? "Connecting…" : "Connect"}
            </Button>
            {!checked.ready && (
              <span className="text-sm text-muted-foreground">
                Fix the failing check first — a run would clone, work, and die at the push.
              </span>
            )}
          </div>
        </div>
      )}

      {result && (
        <div className="mt-5 text-sm">
          <div className="flex items-baseline justify-between gap-4">
            <p className="font-mono text-xs uppercase tracking-wider text-ok">
              {result.repo} is connected.
            </p>
            <Button variant="ghost" size="sm" onClick={onClose}>
              Done
            </Button>
          </div>
          {result.provision_run ? (
            <Warming runId={result.provision_run} />
          ) : (
            <p className="mt-1 text-muted-foreground">
              No golden warmed: {result.provision_skipped ?? "provisioning was not started"}. Its
              runs clone and install for themselves, which costs minutes rather than
              correctness.
            </p>
          )}
        </div>
      )}
    </Card>
  )
}

/** Where the goal loop stands for one repo, as a stamp. Nothing for `none` — an absent goal
 *  is the ordinary state, not a status. A goal on a deployment with FACTORY_PLAN off is
 *  labelled as such rather than shown as "active": it looks armed and is not, and the
 *  difference is exactly what the person reading this page needs to know. */
function GoalState({ p }: { p: Project }) {
  if (p.goal_state === "met") return <Badge variant="ok">goal met</Badge>
  if (p.goal_state === "stalled") return <Badge variant="warn">stalled ×{p.plan_stalls}</Badge>
  if (p.goal_state === "active")
    return p.plan_enabled ? (
      <Badge variant="outline">goal active</Badge>
    ) : (
      <Badge variant="muted" title="Set FACTORY_PLAN=1 to let the factory plan toward this goal">
        planning off
      </Badge>
    )
  return null
}

/** Read, edit and re-arm a repo's goal — the endstate the factory plans toward when this
 *  repo's queue runs dry. Each cell manages its own editor; the register row is small and
 *  the 15-second projects poll refreshes the read view underneath it. */
function GoalCell({ p, onChanged }: { p: Project; onChanged: () => void }) {
  const [editing, setEditing] = useState(false)
  const [text, setText] = useState("")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function act(work: () => Promise<unknown>) {
    setBusy(true)
    setError(null)
    try {
      await work()
      setEditing(false)
      onChanged()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  if (editing) {
    return (
      <div className="flex min-w-64 flex-col gap-2 py-1">
        <textarea
          className="min-h-20 w-full border border-input bg-background p-2 font-mono text-xs text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring"
          value={text}
          disabled={busy}
          placeholder="The endstate: what should this project be when it is done?"
          onChange={(e) => setText(e.target.value)}
        />
        <div className="flex items-center gap-2">
          <Button size="sm" disabled={busy} onClick={() => act(() => setGoal(p.repo, text))}>
            {busy ? "Saving…" : "Save"}
          </Button>
          <Button variant="ghost" size="sm" disabled={busy} onClick={() => setEditing(false)}>
            Cancel
          </Button>
          {error && <span className="font-mono text-[10px] text-bad">{error}</span>}
        </div>
      </div>
    )
  }

  return (
    <div className="flex items-center gap-2">
      <GoalState p={p} />
      {p.goal && (
        <span className="max-w-56 truncate text-xs text-muted-foreground" title={p.goal}>
          {p.goal}
        </span>
      )}
      <Button
        variant="ghost"
        size="sm"
        title="The goal loop: when this repo's queue runs dry, a plan run compares it against this text and files the next issues or declares it met."
        onClick={() => {
          setText(p.goal ?? "")
          setEditing(true)
        }}
      >
        {p.goal ? "Edit" : "Set goal"}
      </Button>
      {(p.goal_state === "met" || p.goal_state === "stalled") && (
        <Button
          variant="ghost"
          size="sm"
          disabled={busy}
          title="Back to active: the next dry poll tick plans against this goal again"
          onClick={() => act(() => replan(p.repo))}
        >
          Replan
        </Button>
      )}
      {error && <span className="font-mono text-[10px] text-bad">{error}</span>}
    </div>
  )
}

function Provisioning({ status }: { status: string }) {
  if (status === "ready") return <Badge variant="outline">warm</Badge>
  if (status === "running") return <Badge variant="muted">warming…</Badge>
  if (status === "failed") return <Badge variant="warn">warm-up failed</Badge>
  return null
}

/** Per-repo actions. Disconnect asks twice rather than opening a browser `confirm()`: a modal
 *  dialog blocks the page, and the second click is the same amount of deliberation. */
function Actions({ repo, warm, onChanged }: { repo: string; warm: boolean; onChanged: () => void }) {
  const [busy, setBusy] = useState<string | null>(null)
  const [confirming, setConfirming] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function act(what: string, work: () => Promise<unknown>) {
    setBusy(what)
    setError(null)
    try {
      await work()
      onChanged()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(null)
      setConfirming(false)
    }
  }

  return (
    <div className="flex items-center justify-end gap-2">
      {error && <span className="font-mono text-[10px] text-bad">{error}</span>}
      <Button
        variant="ghost"
        size="sm"
        title={
          warm
            ? "Rebuild this repo's golden from the base image"
            : "Clone and install this repo into its own golden snapshot"
        }
        disabled={busy !== null}
        onClick={() => act("warm", () => warmGolden(repo))}
      >
        <Flame /> {busy === "warm" ? "Starting…" : warm ? "Rebuild" : "Warm"}
      </Button>
      {warm && (
        <Button
          variant="ghost"
          size="sm"
          title="Delete this repo's golden; its runs fall back to golden-copy"
          disabled={busy !== null}
          onClick={() => act("drop", () => dropGolden(repo))}
        >
          {busy === "drop" ? "Dropping…" : "Drop golden"}
        </Button>
      )}
      <Button
        variant={confirming ? "destructive" : "ghost"}
        size="sm"
        title="Stop watching this repo. Its runs are kept."
        disabled={busy !== null}
        onClick={() => (confirming ? act("remove", () => disconnectRepo(repo)) : setConfirming(true))}
      >
        <Trash2 /> {busy === "remove" ? "Removing…" : confirming ? "Disconnect?" : "Disconnect"}
      </Button>
    </div>
  )
}

export function Projects() {
  const { data: projects, error, refresh } = usePoll<Project[]>("/api/projects", 15000)
  const [connecting, setConnecting] = useState(false)

  return (
    <div>
      <PageHeader
        kicker="Watchlist"
        title="Projects"
        subtitle="Repos the factory watches for labelled issues."
        actions={
          <Button variant="outline" onClick={() => setConnecting((c) => !c)}>
            <Plus /> {connecting ? "Cancel" : "Connect repo"}
          </Button>
        }
      />
      {connecting && <Connect onDone={refresh} onClose={() => setConnecting(false)} />}
      {error && <ErrorNote message={error} />}
      {projects && projects.length === 0 && !connecting && (
        <Empty>No repos connected yet. Connect one — it can take issues immediately.</Empty>
      )}
      {projects && projects.length > 0 && (
        <Card>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Id</TableHead>
                <TableHead>Git Url</TableHead>
                <TableHead>Golden</TableHead>
                <TableHead>Sentry</TableHead>
                <TableHead>Goal</TableHead>
                <TableHead className="text-right">Runs</TableHead>
                <TableHead className="text-right">Successful Runs</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {projects.map((p) => (
                <TableRow key={p.repo}>
                  <TableCell className="whitespace-nowrap font-mono">{p.repo}</TableCell>
                  <TableCell className="whitespace-nowrap">
                    <a
                      href={gitUrl(p.repo)}
                      target="_blank"
                      rel="noreferrer"
                      className="font-mono text-primary underline-offset-4 hover:underline"
                    >
                      {gitUrl(p.repo)}
                    </a>
                  </TableCell>
                  <TableCell className="whitespace-nowrap font-mono">
                    {p.golden || "—"}
                    <span className="ml-2">
                      <Provisioning status={p.provision_status} />
                    </span>
                  </TableCell>
                  {/* Which Sentry project mirrors this repo, and whether its wiring issue
                      exists yet. The DSN itself lives on the register and in that issue —
                      a name is what a human scans a table for. */}
                  <TableCell className="whitespace-nowrap font-mono text-xs">
                    {p.sentry_project ? (
                      <>
                        {p.sentry_project}
                        {p.sentry_wiring_issue && (
                          <span className="ml-2 text-muted-foreground">
                            #{p.sentry_wiring_issue}
                          </span>
                        )}
                      </>
                    ) : (
                      "—"
                    )}
                  </TableCell>
                  <TableCell>
                    <GoalCell p={p} onChanged={refresh} />
                  </TableCell>
                  <TableCell className="text-right font-mono tabular-nums">{p.runs}</TableCell>
                  <TableCell className="text-right font-mono tabular-nums text-ok">{p.succeeded}</TableCell>
                  <TableCell>
                    <Actions repo={p.repo} warm={p.warm} onChanged={refresh} />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Card>
      )}
    </div>
  )
}
