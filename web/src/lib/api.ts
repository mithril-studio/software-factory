import { useCallback, useEffect, useRef, useState } from "react"

// ---- shapes mirrored from the FastAPI JSON API ----

export type Run = {
  id: string
  repo: string
  issue_number: number
  issue_title: string | null
  branch: string | null
  status: string
  /** `build` for every v1 run; older rows may carry review, plan, learn and others. */
  kind: string
  agent: string | null
  golden: string | null
  tokens_in: number | null
  tokens_out: number | null
  cost_usd: number | null
  /** 1 when the run passed the soft per-run budget. It kept running. */
  over_budget: number | null
  exit_code: number | null
  pr_url: string | null
  error: string | null
  vm_name: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
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
  /** When it was connected. */
  added_at: string | null
  /** The snapshot this repo's runs boot. */
  golden: string
  /** True when that snapshot is this repo's own warm golden rather than `golden-copy`. */
  warm: boolean
  /** Where its pull requests go: its own setting, else the factory default. */
  target_branch: string
  /** The repo's own setting, or null when it follows the factory default. */
  base_branch: string | null
  runs: number
  succeeded: number
  failed: number
  active: number
  last_run: string | null
}

export type Check = {
  name: string
  ok: boolean
  detail: string
  fatal: boolean
}

export type Preflight = {
  repo: string
  ready: boolean
  checks: Check[]
}

export type Connected = {
  repo: string
  checks: Check[]
}

export type Golden = {
  name: string
  /** The repo slug it was warmed for, `""` for the base image, null if not a golden. */
  repo: string | null
  status: string
  version: string
}

export type Machine = {
  name: string
  status: string | null
  role: "run" | "other"
  orphan: boolean
}

export type Config = {
  repos: string[]
  target_branch: string
  sequential: boolean
  run_budget_eur: number
  max_concurrent: number
  poll_enabled: boolean
  poll_interval: number
  missing: string[]
  problems: string[]
}

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

export type Digest = {
  window: { days: number; since: string; repo: string | null }
  rejections: DigestCluster[]
  outcomes: {
    repo: string
    runs: number
    issues: number
    shipped: number
    failed_runs: number
    shipped_nothing: number
    retries: number
    max_cycle: number
    context_versions: number
  }[]
  failures: DigestCluster[]
  tool_errors: { tool: string; failures: number; runs: number; example: string | null }[]
  retrieval: {
    repo: string
    outcome: "went_wrong" | "went_fine"
    runs: number
    primed: number
    records_opened: number
    avg_index_size: number
  }[]
  skills: { repo: string; skill: string; loads: number; runs: number; last_loaded: string | null }[]
  cost: { repo: string; spend: number; wasted: number; shipped: number }[]
  truncated: Record<string, number>
}

export type DigestCluster = {
  signature: string
  count: number
  repos: string[]
  issues: number[]
  example: string
  run_ids: string[]
  last_seen: string | null
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
    /** Model calls whose model has no price row: they count as $0 in every derived figure. */
    unpriced_calls?: number
  }
  by_model: { model: string; calls: number; derived_cost_usd: number; priced: number }[]
  tools: ToolStat[]
}

export const TERMINAL = ["succeeded", "failed", "cancelled"]

/** A dropped session anywhere in the app funnels the user back to the login screen. */
function onUnauthorized() {
  if (window.location.pathname !== "/login") {
    window.location.assign("/login")
  }
}

/** The server's explanation of a refusal. `POST /api/repos` answers a blocking preflight
 *  with `{detail: {message, checks}}`, so `.detail` is not always a string. */
async function detail(resp: Response): Promise<string> {
  try {
    const body = (await resp.json()).detail
    if (typeof body === "string") return body
    if (body && typeof body === "object" && typeof body.message === "string") return body.message
    return resp.statusText
  } catch {
    return resp.statusText
  }
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(url, init)
  if (!resp.ok) {
    if (resp.status === 401) onUnauthorized()
    throw new Error(await detail(resp))
  }
  return resp.json()
}

export function get<T>(url: string): Promise<T> {
  return request<T>(url)
}

export function post<T>(url: string, body?: unknown): Promise<T> {
  return request<T>(url, {
    method: "POST",
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  })
}

export function patch<T>(url: string, body: unknown): Promise<T> {
  return request<T>(url, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  })
}

export function del<T>(url: string): Promise<T> {
  return request<T>(url, { method: "DELETE" })
}

// ---- runs ----

/** Label an issue `agent:queued`, the same as doing it on GitHub. The poller starts it. */
export function queueIssue(repo: string, number: number): Promise<{ ok: boolean }> {
  return post<{ ok: boolean }>("/api/plan/queue", { repo, number })
}

export function cancelRun(runId: string): Promise<{ ok: boolean }> {
  return post<{ ok: boolean }>(`/api/runs/${runId}/cancel`)
}

// ---- repos ----

/** One repo the control plane's GitHub token can see. For the connect picker only. */
export type GithubRepo = {
  full_name: string
  private: boolean
  archived: boolean
  default_branch: string
  pushed_at: string | null
  /** What the *account* may do, which is not what the token may do. Enough to grey a row. */
  can_push: boolean
  /** Already in the register, so connecting it again would 409. */
  connected: boolean
}

/** The repos this token can see. `error` rather than a throw: the picker still takes free text. */
export function githubRepos(): Promise<{ repos: GithubRepo[]; error: string | null }> {
  return get<{ repos: GithubRepo[]; error: string | null }>("/api/github/repos")
}

/** Ask whether a repo could be dispatched to. Read-only; safe to call on anything. */
export function preflight(repo: string): Promise<Preflight> {
  return get<Preflight>(`/api/preflight?repo=${encodeURIComponent(repo)}`)
}

/** Watch a repo from now on. Throws with the failing check when preflight blocks it. */
export function connectRepo(repo: string): Promise<Connected> {
  return post<Connected>("/api/repos", { repo })
}

export function disconnectRepo(repo: string): Promise<{ removed: boolean }> {
  return del<{ removed: boolean }>(`/api/repos/${repo}`)
}

/** Point a repo's pull requests at another branch, or back at the default with "". */
export function setTargetBranch(repo: string, branch: string): Promise<{ base_branch: string | null }> {
  return patch<{ base_branch: string | null }>(`/api/repos/${repo}`, { base_branch: branch })
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

/** Fetch `url` once, then re-poll every `intervalMs` if given.
 *
 *  A response is dropped if `url` changed while it was in flight, so a slow answer for the
 *  previous filter can never overwrite the current one. */
export function usePoll<T>(url: string, intervalMs?: number) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const savedUrl = useRef(url)
  savedUrl.current = url

  const refresh = useCallback(async () => {
    const asked = savedUrl.current
    try {
      const d = await get<T>(asked)
      if (asked !== savedUrl.current) return
      setData(d)
      setError(null)
    } catch (e) {
      if (asked !== savedUrl.current) return
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      if (asked === savedUrl.current) setLoading(false)
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

/** Subscribe to a run's SSE log stream; returns the accumulated lines and whether it's done.
 *
 *  The server tags every line with its byte offset, and EventSource sends the last one back
 *  when it reconnects, so a dropped connection resumes instead of duplicating the log. */
export function useRunLog(runId: string) {
  const [lines, setLines] = useState<string[]>([])
  const [done, setDone] = useState(false)

  useEffect(() => {
    setLines([])
    setDone(false)
    const source = new EventSource(`/api/runs/${runId}/stream`)
    let pending: string[] = []
    let frame = 0
    // Batch lines per animation frame: a fast agent emits hundreds per second, and one state
    // update per line re-renders the whole log each time.
    const flush = () => {
      frame = 0
      const batch = pending
      pending = []
      setLines((prev) => prev.concat(batch))
    }
    source.onmessage = (e) => {
      pending.push(JSON.parse(e.data) as string)
      if (!frame) frame = requestAnimationFrame(flush)
    }
    source.addEventListener("done", () => {
      if (frame) cancelAnimationFrame(frame)
      flush()
      setDone(true)
      source.close()
    })
    source.onerror = () => {
      /* EventSource retries on its own, resuming from the last id */
    }
    return () => {
      if (frame) cancelAnimationFrame(frame)
      source.close()
    }
  }, [runId])

  return { lines, done }
}
