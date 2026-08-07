import { NavLink, Outlet } from "react-router-dom"
import { Activity, Boxes, FolderGit2, ListChecks, BarChart3, Factory } from "lucide-react"
import { cn } from "@/lib/utils"
import { usePoll, type Config } from "@/lib/api"

const NAV = [
  { to: "/", label: "Runs", icon: Activity, end: true },
  { to: "/plan", label: "Plan", icon: ListChecks },
  { to: "/projects", label: "Projects", icon: FolderGit2 },
  { to: "/agents", label: "Agents", icon: Boxes },
  { to: "/telemetry", label: "Telemetry", icon: BarChart3 },
]

export function Layout() {
  const { data: config } = usePoll<Config>("/api/config", 30000)

  return (
    <div className="flex min-h-screen">
      <aside className="fixed inset-y-0 left-0 flex w-60 flex-col border-r border-border bg-card/40">
        <div className="flex items-center gap-2 px-5 py-5">
          <Factory className="size-5 text-primary" />
          <span className="font-semibold tracking-tight">software factory</span>
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
      </aside>
      <main className="ml-60 flex-1 px-8 py-8">
        <div className="mx-auto max-w-6xl">
          <Outlet />
        </div>
      </main>
    </div>
  )
}
