import { NavLink, Outlet, useNavigate } from "react-router-dom"
import { Activity, Boxes, Bot, FolderGit2, ListChecks, BarChart3, Sun, Moon, LogOut } from "lucide-react"
import { cn } from "@/lib/utils"
import { useTheme } from "@/lib/theme"
import { logout } from "@/lib/api"

const NAV = [
  { to: "/", label: "Runs", icon: Activity, end: true },
  { to: "/plan", label: "Plan", icon: ListChecks },
  { to: "/projects", label: "Projects", icon: FolderGit2 },
  { to: "/agents", label: "Agents", icon: Bot },
  { to: "/telemetry", label: "Telemetry", icon: BarChart3 },
]

export function Layout() {
  const { theme, toggle } = useTheme()
  const navigate = useNavigate()

  async function onLogout() {
    await logout()
    navigate("/login", { replace: true })
  }

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
          <button
            type="button"
            onClick={onLogout}
            className="flex w-full items-center gap-3 rounded-md px-3 py-2 text-sm font-medium text-muted-foreground transition-colors hover:bg-secondary/60 hover:text-foreground"
          >
            <LogOut className="size-4" />
            Log out
          </button>
        </div>
      </aside>
      <main className="ml-60 flex-1 px-[30px] py-8">
        <Outlet />
      </main>
    </div>
  )
}
