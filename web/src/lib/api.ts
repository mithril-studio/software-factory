import { useCallback, useEffect, useRef, useState } from "react"

// ---- shapes mirrored from the FastAPI JSON API ----

export type Run = {
  id: string
  repo: string
  issue_number: number
  issue_title: string | null
  branch: string | null
  status: string
  attempt: number | null
  kind: string
  verdict: string | null
  agent: string | null
  tokens_in: number | null
  tokens_out: number | null
  cost_usd: number | null
  exit_code: number | null
  pr_url: string | null
  error: string | null
  vm_name: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
}

// A run's `status` is the *process* outcome — did the agent complete — which is not the same
// question as whether the result was good. A reviewer that runs cleanly and rejects the pull
// request records `succeeded` with the reason in `error`, so in the runs list a rejected
// review was indistinguishable from an approved one: both green. Fold the verdict back in
// here rather than at each call site, so the list and the detail page cannot disagree.
export function runOutcome(r: Run): string {
  if (r.kind === "review" && r.status === "succeeded" && r.error) return "changes requested"
  return r.status
}

export type PlanIssue = {
  repo: string
  number: number
  title: string
  url: string
  state: string
  labels: string[]
}

export type Project = {
  repo: string
  /** The agent that takes this repo's issues. */
  agent: string
  /** The snapshot that agent actually boots for this repo. */
  golden: string
  /** True when that snapshot is this repo's own warm tier rather than the bare agent image.
   *  A speed-up only — every run installs the repo for itself either way. */
  warm: boolean
  runs: number
  succeeded: number
  failed: number
  active: number
  last_run: string | null
}

/** A golden snapshot the factory can dispatch onto, as the refresh loop last saw it.
 *
 *  Not a machine. Goldens stopped being machines when they became snapshots, which is why
 *  this and `Machine` are two types served from two endpoints instead of one list that could
 *  answer neither question fully. */
export type Agent = {
  agent: string
  snapshot: string
  version: string | null
  /** The telemetry adapter this golden's stream is read with, from its manifest. */
  events: string | null
  agent_version: string | null
  /** What the last run on this snapshot did. False with no error means no run has used it. */
  ok: boolean
  error: string | null
  /** When a run last finished on it having produced usage — the only proof its credentials
   *  still work. Null means unproven, not broken. */
  verified_at: string | null
  /** True when this is the agent a repo that names none of its own gets. */
  default: boolean
}

/** A boxd machine in the fleet. */
export type Machine = {
  name: string
  status: string | null
  /** From the VM name prefixes the reaper sweeps on. `other` is anything the factory did not
   *  create — including a golden still held as a machine, which is now a rollback artefact. */
  role: "run" | "review" | "other"
  /** A run VM with no run behind it any more. What Reconcile reaps. */
  orphan: boolean
}

export type Config = {
  repos: string[]
  /** Each watched repo with the agent that takes its issues. */
  watched: { repo: string; agent: string }[]
  /** Every agent the snapshot fleet can name. */
  agents: string[]
  agent_default: string
  max_concurrent: number
  max_attempts: number
  poll_enabled: boolean
  poll_interval: number
  /** A setting nobody filled in. Blocks starting a run. */
  missing: string[]
  /** A complete configuration with nothing to run on — an agent with no snapshot yet. */
  problems: string[]
}

/** Cost split by token class. Cache reads dominate real runs — that is the finding
 *  the old one-row-per-run ledger had no column to show. */
export type Composition = {
  input: number
  output: number
  cache_read: number
  cache_write: number
}

export type DaySpend = { day: string; derived_cost_usd: number; runs: number }

export type ToolStat = {
  tool: string
  calls: number
  failures: number
  duration_ms: number
}

/** Per repo. `shipped` counts issues that produced a pull request; `wasted` is spend
 *  on runs that never did — the two halves of what a merged PR actually costs. */
export type Economics = {
  repo: string
  issues: number
  runs: number
  spend: number
  shipped: number
  wasted: number
}

export type Telemetry = {
  composition: Composition
  spend_by_day: DaySpend[]
  tools: ToolStat[]
  economics: Economics[]
}

export type RunTelemetry = {
  totals: {
    calls?: number
    turns?: number
    input_tokens?: number
    output_tokens?: number
    cache_read_tokens?: number
    cache_write_tokens?: number
    derived_cost_usd?: number
  }
  by_model: { model: string; calls: number; derived_cost_usd: number }[]
  tools: ToolStat[]
}

export const TERMINAL = ["succeeded", "failed", "cancelled"]

/** A dropped session anywhere in the app funnels the user back to the login screen. */
function onUnauthorized() {
  if (window.location.pathname !== "/login") {
    window.location.assign("/login")
  }
}

async function detail(resp: Response): Promise<string> {
  try {
    return (await resp.json()).detail ?? resp.statusText
  } catch {
    return resp.statusText
  }
}

async function get<T>(url: string): Promise<T> {
  const resp = await fetch(url)
  if (!resp.ok) {
    if (resp.status === 401) onUnauthorized()
    throw new Error(await detail(resp))
  }
  return resp.json()
}

export async function post<T>(url: string, body?: unknown): Promise<T> {
  const resp = await fetch(url, {
    method: "POST",
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  })
  if (!resp.ok) {
    if (resp.status === 401) onUnauthorized()
    throw new Error(await detail(resp))
  }
  return resp.json()
}

// ---- auth ----

/** Log in. Throws with the server's message on bad credentials; no redirect. */
export async function login(email: string, password: string): Promise<void> {
  const resp = await fetch("/api/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  })
  if (!resp.ok) throw new Error(await detail(resp))
}

export async function logout(): Promise<void> {
  await fetch("/api/logout", { method: "POST" })
}

/** Whether the current session cookie is valid. Never redirects — used to decide routing. */
export async function checkAuth(): Promise<boolean> {
  try {
    const resp = await fetch("/api/me")
    if (!resp.ok) return false
    return (await resp.json()).authenticated === true
  } catch {
    return false
  }
}

/** Fetch `url` once, then re-poll every `intervalMs` if given. */
export function usePoll<T>(url: string, intervalMs?: number) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const savedUrl = useRef(url)
  savedUrl.current = url

  const refresh = useCallback(async () => {
    try {
      const d = await get<T>(savedUrl.current)
      setData(d)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    setLoading(true)
    refresh()
    if (!intervalMs) return
    const id = setInterval(refresh, intervalMs)
    return () => clearInterval(id)
  }, [url, intervalMs, refresh])

  return { data, error, loading, refresh }
}

/** Subscribe to a run's SSE log stream; returns the accumulated lines and whether it's done. */
export function useRunLog(runId: string) {
  const [lines, setLines] = useState<string[]>([])
  const [done, setDone] = useState(false)

  useEffect(() => {
    setLines([])
    setDone(false)
    const source = new EventSource(`/api/runs/${runId}/stream`)
    source.onmessage = (e) => {
      const line = JSON.parse(e.data) as string
      setLines((prev) => [...prev, line])
    }
    source.addEventListener("done", () => {
      setDone(true)
      source.close()
    })
    source.onerror = () => {
      /* EventSource retries on its own */
    }
    return () => source.close()
  }, [runId])

  return { lines, done }
}
