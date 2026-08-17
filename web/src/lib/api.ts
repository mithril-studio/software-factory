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
  runs: number
  succeeded: number
  failed: number
  active: number
  last_run: string | null
}

export type Agent = {
  name: string
  status: string | null
  role: "golden" | "run"
  is_golden: boolean
  orphan: boolean
}

export type Config = {
  repos: string[]
  golden: string
  max_concurrent: number
  max_attempts: number
  poll_enabled: boolean
  poll_interval: number
  missing: string[]
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
