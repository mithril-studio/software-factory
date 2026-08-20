import { useState, type FormEvent } from "react"
import { useNavigate } from "react-router-dom"
import { Boxes } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { login } from "@/lib/api"

export function Login() {
  const navigate = useNavigate()
  const [email, setEmail] = useState("")
  const [password, setPassword] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await login(email, password)
      navigate("/", { replace: true })
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      {/* The accent shadow, used here and on the lead stat tile and nowhere else: it
          marks the one thing on the screen the reader is meant to act on. */}
      <div className="w-full max-w-sm border border-border bg-card p-optical-lg shadow-hard-accent">
        <div className="flex items-center gap-2.5">
          <Boxes className="size-7 text-primary" />
          <span className="font-serif text-4xl leading-none text-foreground">Factory</span>
        </div>
        <div className="eyebrow mt-2.5 mb-7 text-muted-foreground">Issue in · PR out</div>
        <form onSubmit={onSubmit} className="flex flex-col gap-4">
          <div className="flex flex-col gap-1.5">
            <label htmlFor="email" className="eyebrow text-muted-foreground">
              Email
            </label>
            <Input
              id="email"
              type="email"
              autoComplete="username"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <label htmlFor="password" className="eyebrow text-muted-foreground">
              Password
            </label>
            <Input
              id="password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </div>
          {error && <p className="font-mono text-xs text-bad">{error}</p>}
          <Button type="submit" disabled={busy} className="mt-1">
            {busy ? "Signing in…" : "Sign in"}
          </Button>
        </form>
      </div>
    </div>
  )
}
