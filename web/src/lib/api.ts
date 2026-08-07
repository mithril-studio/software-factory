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

export const TERMINAL = ["succeeded", "failed", "cancelled"]

async function get<T>(url: string): Promise<T> {
  const resp = await fetch(url)
  if (!resp.ok) {
    let detail = resp.statusText
    try {
      detail = (await resp.json()).detail ?? detail
    } catch {
      /* keep statusText */
    }
    throw new Error(detail)
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
    let detail = resp.statusText
    try {
      detail = (await resp.json()).detail ?? detail
    } catch {
      /* keep statusText */
    }
    throw new Error(detail)
  }
  return resp.json()
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
