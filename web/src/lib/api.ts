import { useCallback, useEffect, useRef, useState } from "react"

// ---- shapes mirrored from the FastAPI JSON API ----

export type Run = {
  id: string
  repo: string
  issue_number: number
  issue_title: string | null
  branch: string | null
  status: string
  /** Dispatches of this run inside its cycle. A crash retry increments it. */
  attempt: number | null
  /** Which pass over the pull request this run belongs to. A review that sends the change
   *  back opens the next one. Not the same number as `attempt`, and it used to be. */
  cycle: number | null
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

/** What a review run's `error` column says happened, as written by `control/runner.py`.
 *  Keep in step with the constants there — `review_outcome_test.py` checks that they exist. */
const REVIEW_OUTCOME: [string, string][] = [
  ["changes requested: ", "changes requested"],
  ["ci red: ", "ci red"],
  ["not merged: ", "not merged"],
]

// A run's `status` is the *process* outcome — did the agent complete — which is not the same
// question as whether the result was good. A reviewer that runs cleanly and rejects the pull
// request records `succeeded` with the reason in `error`, so in the runs list a rejected
// review was indistinguishable from an approved one: both green.
//
// This used to read "any error at all means the reviewer refused", which was wrong in the
// worst available direction. An *approved* review whose pull request then failed CI also
// writes to `error`, and so does one that could not be merged — both were rendered "changes
// requested", saying the reviewer rejected a change it had signed off. Read the tag the
// server writes instead of inferring from the column being non-empty. Folded in here rather
// than at each call site, so the list and the detail page cannot disagree.
export function runOutcome(r: Run): string {
  // A CI phase never "ran" anywhere, so `failed` would be read as a dispatch that broke.
  // Say what it actually is, in the same words the review path already uses for it, so the
  // phase and the review that reports it do not describe one event two ways.
  if (r.kind === "ci") return r.status === "failed" ? "ci red" : r.status
  // A plan run that completed without filing issues or declaring the goal met records
  // `succeeded` with the reason tagged in `error` — the same status/outcome split as a
  // review that requested changes, read the same way.
  if (r.kind === "plan" && r.status === "succeeded" && r.error?.startsWith("planned nothing: "))
    return "planned nothing"
  if (r.kind !== "review" || r.status !== "succeeded" || !r.error) return r.status
  for (const [prefix, outcome] of REVIEW_OUTCOME) {
    if (r.error.startsWith(prefix)) return outcome
  }
  // An older row, written before the tags existed. Every one of them is a refusal — the two
  // approved-but-not-merged paths came later — so this stays the honest reading of history.
  return "changes requested"
}

/** One unit of work: every dispatch that went into a single pass at a single issue.
 *  `key` groups them — see ATTEMPT_KEY in `control/db.py`. Phases ascend by time. */
export type Attempt = {
  key: string
  phases: Run[]
}

// What the whole attempt amounts to, which is the question the dispatch log was not
// answering. The rule is only "whatever its last phase came to", and that works because the
// phases are appended in causal order: a build pushes, CI judges what was pushed, the review
// decides what to do about it. So a green build under a red CI no longer reads as success —
// the attempt takes the CI phase's verdict, because that is the one that came last and is
// therefore the one that is still true.
//
// Anything unfinished wins over all of it. An attempt whose review is still running is
// "running" even though its build succeeded twenty minutes ago; reporting the build's
// outcome as the attempt's is exactly the confusion this replaces.
export function attemptOutcome(a: Attempt): string {
  // An unrecognised status counts as unfinished, deliberately: a live row is a better
  // wrong answer than an outcome the run never actually reached.
  if (a.phases.some((p) => !TERMINAL.includes(p.status))) return "running"
  const last = a.phases[a.phases.length - 1]
  return last ? runOutcome(last) : "unknown"
}

/** Sum a numeric column across an attempt's phases, or null when no phase reported one.
 *  Null rather than 0 because a provisioning run and a run that cost nothing are different
 *  facts, and the table renders the first as an em dash. */
export function attemptTotal(a: Attempt, pick: (r: Run) => number | null): number | null {
  const given = a.phases.map(pick).filter((n): n is number => n != null)
  return given.length ? given.reduce((x, y) => x + y, 0) : null
}

/** The pull request the attempt produced. Later phases carry it and the build that opened it
 *  may not have by the time it was written, so read from the end. */
export function attemptPr(a: Attempt): string | null {
  for (let i = a.phases.length - 1; i >= 0; i--) {
    if (a.phases[i].pr_url) return a.phases[i].pr_url
  }
  return null
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
  /** What provisioning a warm golden for this repo did — `none`, `running`, `ready`,
   *  `failed`. A report, never a gate: a repo dispatches onto `golden-copy` either way. */
  provision_status: string
  /** The snapshot this repo's runs actually boot. */
  golden: string
  /** True when that snapshot is this repo's own warm tier rather than the `golden-copy` base.
   *  A speed-up only — every run installs the repo for itself either way. */
  warm: boolean
  runs: number
  succeeded: number
  failed: number
  active: number
  last_run: string | null
  /** The endstate this repo is being built toward, or null when nobody has set one. */
  goal: string | null
  /** Where the goal loop stands: `none` (no goal), `active` (planning may fire when the
   *  queue runs dry), `met` (a plan run verified the endstate against an empty queue), or
   *  `stalled` (consecutive fruitless plans — a human decides what happens next). */
  goal_state: "none" | "active" | "met" | "stalled"
  /** Consecutive fruitless plan runs. Meaningful mostly beside `stalled`. */
  plan_stalls: number
  last_planned_at: string | null
  /** Whether this deployment runs the goal loop at all (FACTORY_PLAN). A goal set while
   *  this is false is a note on the register, and the UI should say so. */
  plan_enabled: boolean
}

/** One preflight question and its answer.
 *
 *  `ok: false` with `fatal: false` is a warning, not a failure — a repo with no `.factory.md`
 *  still builds, it just builds with less context. Only a fatal one blocks connecting. */
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

/** What `POST /api/repos` answers with.
 *
 *  `provision_run` is the golden-warming run it started, if it could start one.
 *  `provision_skipped` is why it could not — almost always "this repo names no setup command".
 *  Neither is a failure: the repo is watched and dispatchable either way, on `golden-copy`. */
export type Connected = {
  repo: string
  checks: Check[]
  provision_run: string | null
  provision_skipped: string | null
}

/** A golden snapshot the factory can dispatch onto, as the refresh loop last saw it.
 *
 *  Not a machine. Goldens stopped being machines when they became snapshots, which is why
 *  this and `Machine` are two types served from two endpoints instead of one list that could
 *  answer neither question fully. */
export type Golden = {
  snapshot: string
  /** The repo slug the name carries, null for the base image every run falls back to. */
  repo: string | null
  base: boolean
  /** Which agent the image launches, from the manifest it announced into its last run — not
   *  from its name, which says nothing about the agent any more. Null until a run has read one. */
  agent: string | null
  version: string | null
  /** What boxd says the snapshot is doing: `ready`, `pending`. */
  status: string | null
  /** Whether a run could boot it *now*. Not `status === "ready"`: a re-save is `pending` while
   *  its previous version stays restorable, and a first capture is `pending` with nothing
   *  behind it at all. Having a version is the question. */
  ready: boolean
  /** The telemetry adapter this golden's stream is read with, from its manifest. */
  events: string | null
  agent_version: string | null
  /** What the last run on this snapshot did. False with no error means no run has used it. */
  ok: boolean
  error: string | null
  /** When a run last finished on it having produced usage — the only proof its credentials
   *  still work. Null means unproven, not broken. */
  verified_at: string | null
}

/** A boxd machine in the fleet. */
export type Machine = {
  name: string
  status: string | null
  /** From the VM name prefixes the reaper sweeps on. `other` is anything the factory did not
   *  create — including a golden still held as a machine, which is now a rollback artefact. */
  role: "run" | "review" | "provision" | "other"
  /** A run VM with no run behind it any more. What Reconcile reaps. */
  orphan: boolean
}

export type Config = {
  repos: string[]
  max_concurrent: number
  max_attempts: number
  poll_enabled: boolean
  poll_interval: number
  /** A setting nobody filled in. Blocks starting a run. */
  missing: string[]
  /** A complete configuration with nothing to run on — no `golden-copy` to fall back to. */
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

/** The server's explanation of a refusal.
 *
 *  `POST /api/repos` answers a blocking preflight with `{detail: {message, checks}}` rather
 *  than a sentence, because the caller wants to render which check failed. Reading `.detail`
 *  blindly would put `[object Object]` in front of the user at exactly that moment. */
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

export async function del<T>(url: string): Promise<T> {
  const resp = await fetch(url, { method: "DELETE" })
  if (!resp.ok) {
    if (resp.status === 401) onUnauthorized()
    throw new Error(await detail(resp))
  }
  return resp.json()
}

export async function patch<T>(url: string, body: unknown): Promise<T> {
  const resp = await fetch(url, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  })
  if (!resp.ok) {
    if (resp.status === 401) onUnauthorized()
    throw new Error(await detail(resp))
  }
  return resp.json()
}

// ---- repos ----

/** One repo the control plane's GitHub token can see. For the connect picker only — what a
 *  repo may actually do is what preflight asks, not what this says. */
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

/** The repos this deployment's token can see, most recently pushed first.
 *
 *  `error` rather than a throw: the picker is a convenience over a field that still accepts
 *  free text, so a GitHub outage should cost the dropdown and nothing else. */
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

/** Warm this repo's golden now. Also how a stale one is refreshed — provisioning always
 *  rebuilds from the base rather than updating a snapshot in place. */
export function warmGolden(repo: string): Promise<{ run_id: string }> {
  return post<{ run_id: string }>(`/api/repos/${repo}/golden`)
}

/** Drop this repo's golden. Its runs fall back to the base and install for themselves. */
export function dropGolden(repo: string): Promise<{ deleted: boolean }> {
  return del<{ deleted: boolean }>(`/api/repos/${repo}/golden`)
}

/** Set, change or clear a repo's goal. Empty or null clears it; new text activates the
 *  loop; unchanged text is a server-side no-op, so saving twice cannot wake a met goal. */
export function setGoal(repo: string, goal: string | null): Promise<{ goal_state: string }> {
  return patch<{ goal_state: string }>(`/api/repos/${repo}`, { goal })
}

/** Put a `met` or `stalled` goal back to `active`, so the next dry poll tick plans again. */
export function replan(repo: string): Promise<{ goal_state: string }> {
  return post<{ goal_state: string }>(`/api/repos/${repo}/replan`)
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
