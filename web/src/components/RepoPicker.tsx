import { useEffect, useId, useMemo, useRef, useState } from "react"
import { githubRepos, type GithubRepo } from "@/lib/api"
import { Input } from "@/components/ui/input"

/** Pick a repo to connect, from the ones the control plane's token can actually see.
 *
 *  The field this replaces was a bare `owner/name` text box, which meant connecting a repo
 *  started with recalling a slug exactly — and a typo there is not caught until preflight has
 *  asked GitHub about a repo that does not exist.
 *
 *  It stays a text field. The listing is a convenience layered over free text, never a gate:
 *  a repo created a minute ago, one past the pagination limit, or any repo at all when GitHub
 *  is unreachable, must still be connectable by typing it. So the input owns the value and the
 *  list only ever writes into it.
 *
 *  Built from the `Input` primitive and a plain `<ul>` rather than a combobox library. There
 *  is no popover, listbox or `cmdk` in this app's dependencies, and adding one for a single
 *  dropdown is exactly the kind of weight this build refuses. */
export function RepoPicker({
  value,
  onChange,
  onSubmit,
  disabled,
}: {
  value: string
  onChange: (repo: string) => void
  /** Enter on a highlighted row picks it; Enter on free text submits the form instead. */
  onSubmit: () => void
  disabled?: boolean
}) {
  const [all, setAll] = useState<GithubRepo[] | null>(null)
  const [listError, setListError] = useState<string | null>(null)
  const [open, setOpen] = useState(false)
  const [active, setActive] = useState(0)
  const box = useRef<HTMLDivElement>(null)
  const listId = useId()

  // Fetched once, when the panel mounts. The set of repos an account can see does not move
  // during the seconds this form is open, and re-fetching per keystroke would spend a GitHub
  // call on filtering that is a substring match.
  useEffect(() => {
    let live = true
    githubRepos()
      .then((r) => {
        if (!live) return
        setAll(r.repos)
        setListError(r.error)
      })
      .catch((e) => live && setListError(e instanceof Error ? e.message : String(e)))
    return () => {
      live = false
    }
  }, [])

  useEffect(() => {
    function away(e: MouseEvent) {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener("mousedown", away)
    return () => document.removeEventListener("mousedown", away)
  }, [])

  const matches = useMemo(() => {
    const needle = value.trim().toLowerCase()
    const found = (all ?? []).filter((r) => r.full_name.toLowerCase().includes(needle))
    // Enough to scroll, not enough to bury the field. The filter is what narrows a long list.
    return found.slice(0, 8)
  }, [all, value])

  const showing = open && matches.length > 0
  // A row already in the register would 409, so it is shown and not selectable — hiding it
  // would read as "that repo is gone" to whoever went looking for it.
  const pickable = (r: GithubRepo) => !r.connected

  function pick(r: GithubRepo) {
    if (!pickable(r)) return
    onChange(r.full_name)
    setOpen(false)
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Escape") {
      setOpen(false)
      return
    }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault()
      setOpen(true)
      setActive((i) => {
        const step = e.key === "ArrowDown" ? 1 : -1
        const next = i + step
        return next < 0 ? matches.length - 1 : next >= matches.length ? 0 : next
      })
      return
    }
    if (e.key === "Enter") {
      if (showing && matches[active] && pickable(matches[active])) {
        e.preventDefault()
        pick(matches[active])
        return
      }
      // Free text, or a highlighted row that cannot be picked: let the form submit and have
      // preflight answer, which is the same path a typed slug takes.
      e.preventDefault()
      setOpen(false)
      onSubmit()
    }
  }

  return (
    <div ref={box} className="relative flex flex-1 flex-col gap-1.5">
      <label htmlFor="repo" className="eyebrow text-muted-foreground">
        Repository
      </label>
      <Input
        id="repo"
        role="combobox"
        aria-expanded={showing}
        aria-controls={listId}
        aria-autocomplete="list"
        aria-activedescendant={showing ? `${listId}-${active}` : undefined}
        autoComplete="off"
        value={value}
        placeholder={all === null && !listError ? "loading your repos…" : "owner/name"}
        autoFocus
        disabled={disabled}
        onFocus={() => setOpen(true)}
        onKeyDown={onKeyDown}
        onChange={(e) => {
          onChange(e.target.value)
          setActive(0)
          setOpen(true)
        }}
      />
      {showing && (
        <ul
          id={listId}
          role="listbox"
          className="absolute top-full z-20 mt-1 max-h-72 w-full overflow-y-auto border border-border bg-card shadow-hard-sm"
        >
          {matches.map((r, i) => (
            <li
              key={r.full_name}
              id={`${listId}-${i}`}
              role="option"
              aria-selected={i === active}
              aria-disabled={!pickable(r)}
              onMouseEnter={() => setActive(i)}
              onMouseDown={(e) => {
                // mousedown, not click: the input's blur would close the list first.
                e.preventDefault()
                pick(r)
              }}
              className={[
                "flex items-center justify-between gap-3 px-3 py-2 font-mono text-xs",
                pickable(r) ? "cursor-pointer" : "cursor-default opacity-50",
                i === active && pickable(r) ? "bg-muted text-foreground" : "text-muted-foreground",
              ].join(" ")}
            >
              <span className="truncate">{r.full_name}</span>
              <span className="shrink-0 text-[10px] uppercase tracking-wider">
                {r.connected ? "connected" : r.private ? "private" : ""}
              </span>
            </li>
          ))}
        </ul>
      )}
      {listError && (
        <p className="text-xs text-muted-foreground">
          {listError} — type the repo as <span className="font-mono">owner/name</span> instead.
        </p>
      )}
    </div>
  )
}
