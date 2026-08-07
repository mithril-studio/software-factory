import type { Run } from "./api"

export function duration(run: Pick<Run, "started_at" | "finished_at">): string {
  if (!run.started_at) return "—"
  const start = new Date(run.started_at).getTime()
  const end = run.finished_at ? new Date(run.finished_at).getTime() : Date.now()
  const total = Math.max(0, Math.round((end - start) / 1000))
  if (total >= 3600) return `${Math.floor(total / 3600)}h ${Math.floor((total % 3600) / 60)}m`
  if (total >= 60) return `${Math.floor(total / 60)}m ${total % 60}s`
  return `${total}s`
}

export function ago(iso: string | null): string {
  if (!iso) return "—"
  const secs = Math.round((Date.now() - new Date(iso).getTime()) / 1000)
  if (secs < 60) return "just now"
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`
  return `${Math.floor(secs / 86400)}d ago`
}

export function tokens(n: number | null): string {
  if (n == null) return "—"
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`
  return String(n)
}

export function cost(n: number | null): string {
  if (n == null) return "—"
  return `$${n.toFixed(2)}`
}

export function shortId(id: string): string {
  return id.slice(0, 8)
}
