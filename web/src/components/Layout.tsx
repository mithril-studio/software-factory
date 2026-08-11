import { NavLink, Outlet } from "react-router-dom"
import { Activity, Boxes, Bot, FolderGit2, ListChecks, BarChart3, Sun, Moon } from "lucide-react"
import { cn } from "@/lib/utils"
import { usePoll, type Config } from "@/lib/api"
import { useTheme } from "@/lib/theme"

const NAV = [
  { to: "/", label: "Runs", icon: Activity, end: true },
  { to: "/plan", label: "Plan", icon: ListChecks },
  { to: "/projects", label: "Projects", icon: FolderGit2 },
  { to: "/agents", label: "Agents", icon: Bot },
  { to: "/telemetry", label: "Telemetry", icon: BarChart3 },
]

export function Layout() {
  const { data: config } = usePoll<Config>("/api/config", 30000)
  const { theme, toggle } = useTheme()

  return (
    <div className="flex min-h-screen">
      <aside className="fixed inset-y-0 left-0 flex w-60 flex-col border-r border-border bg-card/40">
        <div className="flex items-center gap-2.5 px-5 py-5">
          <Boxes className="size-8 text-primary" />
          <span className="text-3xl font-semibold tracking-tight text-foreground">Factory</span>
        </div>
        <nav className="flex flex-1 flex-col gap-1 px-3">
          {NAV.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors",
                  isActive
                    ? "bg-secondary text-foreground"
                    : "text-muted-foreground hover:bg-secondary/60 hover:text-foreground"
                )
              }
            >
              <Icon className="size-4" />
              {label}
            </NavLink>
          ))}
        </nav>
        <div className="border-t border-border px-5 py-4 text-xs text-muted-foreground">
          {config ? (
            <>
              <div className="truncate">
                golden <span className="font-mono text-foreground">{config.golden || "—"}</span>
              </div>
              <div className="mt-1">
                {config.poll_enabled
                  ? `watching ${config.repos.length} repo${config.repos.length === 1 ? "" : "s"} · ${config.poll_interval}s`
                  : "polling off"}
              </div>
              {config.missing.length > 0 && (
                <div className="mt-2 text-bad">missing: {config.missing.join(", ")}</div>
              )}
            </>
          ) : (
            <span>connecting…</span>
          )}
        </div>
        <div className="border-t border-border px-3 py-3">
          <button
            type="button"
            onClick={toggle}
            aria-label={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
            className="flex w-full items-center gap-3 rounded-md px-3 py-2 text-sm font-medium text-muted-foreground transition-colors hover:bg-secondary/60 hover:text-foreground"
          >
            {theme === "dark" ? <Moon className="size-4" /> : <Sun className="size-4" />}
            {theme === "dark" ? "Dark" : "Light"}
          </button>
        </div>
      </aside>
      <main className="ml-60 flex-1 px-8 py-8">
        <div className="mx-auto max-w-6xl">
          <Outlet />
        </div>
      </main>
    </div>
  )
}
