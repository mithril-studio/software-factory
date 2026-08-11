import { useEffect, useState, type ReactNode } from "react"
import { createBrowserRouter, Navigate, RouterProvider } from "react-router-dom"
import { Layout } from "@/components/Layout"
import { Login } from "@/pages/Login"
import { Runs } from "@/pages/Runs"
import { RunDetail } from "@/pages/RunDetail"
import { Plan } from "@/pages/Plan"
import { Projects } from "@/pages/Projects"
import { Agents } from "@/pages/Agents"
import { Telemetry } from "@/pages/Telemetry"
import { checkAuth } from "@/lib/api"

/** Blocks the protected shell until the session is confirmed; bounces to /login otherwise. */
function RequireAuth({ children }: { children: ReactNode }) {
  const [authed, setAuthed] = useState<boolean | null>(null)

  useEffect(() => {
    checkAuth().then(setAuthed)
  }, [])

  if (authed === null) return null // brief check; avoid flashing the UI or the login page
  if (!authed) return <Navigate to="/login" replace />
  return <>{children}</>
}

const router = createBrowserRouter([
  { path: "/login", element: <Login /> },
  {
    element: (
      <RequireAuth>
        <Layout />
      </RequireAuth>
    ),
    children: [
      { path: "/", element: <Runs /> },
      { path: "/runs/:runId", element: <RunDetail /> },
      { path: "/plan", element: <Plan /> },
      { path: "/projects", element: <Projects /> },
      { path: "/agents", element: <Agents /> },
      { path: "/telemetry", element: <Telemetry /> },
    ],
  },
])

export function App() {
  return <RouterProvider router={router} />
}
