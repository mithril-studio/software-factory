import { useEffect, useState } from "react"

export type Theme = "light" | "dark"

function readStored(): Theme {
  const stored = localStorage.getItem("theme")
  return stored === "light" || stored === "dark" ? stored : "dark"
}

export function useTheme() {
  const [theme, setTheme] = useState<Theme>(readStored)

  useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark")
    localStorage.setItem("theme", theme)
  }, [theme])

  return {
    theme,
    setTheme,
    toggle: () => setTheme((t) => (t === "dark" ? "light" : "dark")),
  }
}
