import { useState, type FormEvent } from "react"
import { Plus, Trash2 } from "lucide-react"
import {
  connectRepo,
  disconnectRepo,
  preflight,
  setTargetBranch,
  usePoll,
  type Connected,
  type Preflight,
  type Project,
} from "@/lib/api"
import { CheckList } from "@/components/CheckList"
import { RepoPicker } from "@/components/RepoPicker"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { PageHeader, Empty, ErrorNote, Loading } from "@/components/Page"

function gitUrl(repo: string): string {
  return `https://github.com/${repo}`
}

/** Connect a repo: pick it, see what preflight says, decide.
 *
 *  Two steps because the checks are the point: warnings (no `.factory.md`) never appear at all
 *  if the only time you see checks is when something blocked. A connected repo can take an
 *  issue immediately. */
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
        <div className="mt-5 flex items-baseline justify-between gap-4 text-sm">
          <p className="font-mono text-xs uppercase tracking-wider text-ok">
            {result.repo} is connected. Label an issue <span className="normal-case">agent:queued</span> to start.
          </p>
          <Button variant="ghost" size="sm" onClick={onClose}>
            Done
          </Button>
        </div>
      )}
    </Card>
  )
}

/** Where this repo's pull requests go, editable in place. Empty follows the factory default. */
function TargetCell({ p, onChanged }: { p: Project; onChanged: () => void }) {
  const [value, setValue] = useState(p.base_branch ?? "")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const dirty = value.trim() !== (p.base_branch ?? "")

  async function onSave(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await setTargetBranch(p.repo, value.trim())
      onChanged()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form onSubmit={onSave} className="flex items-center gap-2">
      <Input
        value={value}
        onChange={(e) => setValue(e.target.value)}
        placeholder={p.target_branch}
        title="The branch pull requests go into. Created from the default branch if it does not exist. Empty follows the factory default."
        className="h-7 w-32 text-xs"
        disabled={busy}
      />
      {dirty && (
        <Button type="submit" variant="ghost" size="sm" disabled={busy}>
          {busy ? "Saving…" : "Save"}
        </Button>
      )}
      {error && <span className="font-mono text-[10px] text-bad">{error}</span>}
    </form>
  )
}

/** Disconnect asks twice rather than opening a browser `confirm()`. */
function Disconnect({ repo, onChanged }: { repo: string; onChanged: () => void }) {
  const [busy, setBusy] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function remove() {
    setBusy(true)
    setError(null)
    try {
      await disconnectRepo(repo)
      onChanged()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
      setConfirming(false)
    }
  }

  return (
    <div className="flex items-center justify-end gap-2">
      {error && <span className="font-mono text-[10px] text-bad">{error}</span>}
      <Button
        variant={confirming ? "destructive" : "ghost"}
        size="sm"
        title="Stop watching this repo. Its runs are kept."
        disabled={busy}
        onClick={() => (confirming ? remove() : setConfirming(true))}
      >
        <Trash2 /> {busy ? "Removing…" : confirming ? "Disconnect?" : "Disconnect"}
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
        subtitle="Repos the factory watches for issues labelled agent:queued."
        actions={
          <Button variant="outline" onClick={() => setConnecting((c) => !c)}>
            <Plus /> {connecting ? "Cancel" : "Connect repo"}
          </Button>
        }
      />
      {connecting && <Connect onDone={refresh} onClose={() => setConnecting(false)} />}
      {error && <ErrorNote message={error} />}
      {!projects && !error && <Loading />}
      {projects && projects.length === 0 && !connecting && (
        <Empty>No repos connected yet. Connect one — it can take issues immediately.</Empty>
      )}
      {projects && projects.length > 0 && (
        <Card>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Repo</TableHead>
                <TableHead>Golden</TableHead>
                <TableHead>PRs go into</TableHead>
                <TableHead className="text-right">Runs</TableHead>
                <TableHead className="text-right">Succeeded</TableHead>
                <TableHead className="text-right">Failed</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {projects.map((p) => (
                <TableRow key={p.repo}>
                  <TableCell className="whitespace-nowrap">
                    <a
                      href={gitUrl(p.repo)}
                      target="_blank"
                      rel="noreferrer"
                      className="font-mono text-primary underline-offset-4 hover:underline"
                    >
                      {p.repo}
                    </a>
                    {p.active > 0 && (
                      <Badge variant="warn" className="ml-2">
                        running
                      </Badge>
                    )}
                  </TableCell>
                  <TableCell className="whitespace-nowrap font-mono">
                    {p.golden || "—"}
                    {p.warm && (
                      <Badge variant="outline" className="ml-2">
                        warm
                      </Badge>
                    )}
                  </TableCell>
                  <TableCell>
                    <TargetCell key={p.base_branch ?? ""} p={p} onChanged={refresh} />
                  </TableCell>
                  <TableCell className="text-right font-mono tabular-nums">{p.runs}</TableCell>
                  <TableCell className="text-right font-mono tabular-nums text-ok">{p.succeeded}</TableCell>
                  <TableCell className="text-right font-mono tabular-nums text-bad">{p.failed}</TableCell>
                  <TableCell>
                    <Disconnect repo={p.repo} onChanged={refresh} />
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
