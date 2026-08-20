import { useState, type FormEvent } from "react"
import { Link } from "react-router-dom"
import { Flame, Plus, Trash2 } from "lucide-react"
import {
  connectRepo,
  disconnectRepo,
  dropGolden,
  preflight,
  usePoll,
  warmGolden,
  type Connected,
  type Preflight,
  type Project,
} from "@/lib/api"
import { CheckList } from "@/components/CheckList"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { PageHeader, Empty, ErrorNote } from "@/components/Page"

function gitUrl(repo: string): string {
  return `https://github.com/${repo}`
}

/** Connect a repo: name it, see what preflight says, decide.
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
function Connect({ onDone }: { onDone: () => void }) {
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

  function onCheck(e: FormEvent) {
    e.preventDefault()
    setResult(null)
    run(() => preflight(repo.trim()), setChecked)
  }

  function onConnect() {
    run(() => connectRepo(repo.trim()), (r) => {
      setResult(r)
      setChecked(null)
      onDone()
    })
  }

  return (
    <Card className="mb-6 p-optical-lg">
      <form onSubmit={onCheck} className="flex items-end gap-3">
        <div className="flex flex-1 flex-col gap-1.5">
          <label htmlFor="repo" className="eyebrow text-muted-foreground">
            Repository
          </label>
          <Input
            id="repo"
            value={repo}
            placeholder="owner/name"
            autoFocus
            onChange={(e) => {
              setRepo(e.target.value)
              setChecked(null)
              setResult(null)
            }}
          />
        </div>
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
          <p className="font-mono text-xs uppercase tracking-wider text-ok">
            {result.repo} is connected.
          </p>
          {result.provision_run ? (
            <p className="mt-1 text-muted-foreground">
              Warming its golden —{" "}
              <Link
                to={`/runs/${result.provision_run}`}
                className="text-primary underline-offset-4 hover:underline"
              >
                watch the log
              </Link>
              . Its runs work on <span className="font-mono">golden-copy</span> until that
              finishes.
            </p>
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
      {connecting && <Connect onDone={refresh} />}
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
