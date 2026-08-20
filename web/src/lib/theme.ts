import { useEffect, useState } from "react"

export type Theme = "light" | "dark"

/** Light is the ground the palette was drawn for — warm paper, black hairlines. The
 *  dark values exist and are honoured, but an unset preference gets the intended one.
 *  Keep in sync with the pre-paint script in index.html. */
function readStored(): Theme {
  const stored = localStorage.getItem("theme")
  return stored === "light" || stored === "dark" ? stored : "light"
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
