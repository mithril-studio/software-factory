import { createBrowserRouter, RouterProvider } from "react-router-dom"
import { Layout } from "@/components/Layout"
import { Runs } from "@/pages/Runs"
import { RunDetail } from "@/pages/RunDetail"
import { Plan } from "@/pages/Plan"
import { Projects } from "@/pages/Projects"
import { Agents } from "@/pages/Agents"
import { Telemetry } from "@/pages/Telemetry"

const router = createBrowserRouter([
  {
    element: <Layout />,
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
