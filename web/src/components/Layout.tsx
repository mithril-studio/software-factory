import { NavLink, Outlet, useNavigate } from "react-router-dom"
import { Activity, Boxes, Bot, FolderGit2, ListChecks, BarChart3, ScrollText, Sprout, Sun, Moon, LogOut } from "lucide-react"
import { cn } from "@/lib/utils"
import { useTheme } from "@/lib/theme"
import { logout } from "@/lib/api"

const NAV = [
  { to: "/", label: "Runs", icon: Activity, end: true },
  { to: "/plan", label: "Plan", icon: ListChecks },
  { to: "/projects", label: "Projects", icon: FolderGit2 },
  { to: "/goldens", label: "Goldens", icon: Bot },
  { to: "/telemetry", label: "Telemetry", icon: BarChart3 },
  { to: "/digest", label: "Digest", icon: ScrollText },
  { to: "/improvements", label: "Improvements", icon: Sprout },
]

/** Sidebar rows: mono, uppercase, square. The active one is a solid terracotta block —
 *  the only saturated fill in the shell, so "where am I" survives peripheral vision. */
function sidebarRow(active: boolean): string {
  return cn(
    "eyebrow flex items-center gap-3 border border-transparent px-3 py-2.5 transition-[background-color,color,transform]",
    active
      ? "border-border bg-primary text-primary-foreground"
      : "text-sidebar-muted hover:translate-x-0.5 hover:bg-white/5 hover:text-sidebar-foreground"
  )
}

export function Layout() {
  const { theme, toggle } = useTheme()
  const navigate = useNavigate()

  async function onLogout() {
    await logout()
    navigate("/login", { replace: true })
  }

  return (
    <div className="flex min-h-screen">
      <aside className="fixed inset-y-0 left-0 flex w-60 flex-col border-r border-border bg-sidebar">
        <div className="border-b border-sidebar-border px-5 py-6">
          <div className="flex items-center gap-2.5">
            <Boxes className="size-7 text-primary" />
            <span className="font-serif text-4xl leading-none text-sidebar-foreground">
              Factory
            </span>
          </div>
          <div className="eyebrow mt-2.5 text-sidebar-muted">Issue in · PR out</div>
        </div>
        <nav className="flex flex-1 flex-col gap-0.5 px-3 py-4">
          {NAV.map(({ to, label, icon: Icon, end }) => (
            <NavLink key={to} to={to} end={end} className={({ isActive }) => sidebarRow(isActive)}>
              <Icon className="size-3.5" />
              {label}
            </NavLink>
          ))}
        </nav>
        <div className="border-t border-sidebar-border px-3 py-3">
          <button
            type="button"
            onClick={toggle}
            aria-label={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
            className={sidebarRow(false) + " w-full"}
          >
            {theme === "dark" ? <Moon className="size-3.5" /> : <Sun className="size-3.5" />}
            {theme === "dark" ? "Dark" : "Light"}
          </button>
          <button type="button" onClick={onLogout} className={sidebarRow(false) + " w-full"}>
            <LogOut className="size-3.5" />
            Log out
          </button>
        </div>
      </aside>
      <main className="ml-60 min-w-0 flex-1 px-8 py-9 lg:px-10">
        <Outlet />
      </main>
    </div>
  )
}
