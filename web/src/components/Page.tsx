import type { ReactNode } from "react"

/**
 * Editorial masthead: a mono kicker, a serif title, then a black rule the whole page
 * hangs from. The rule is what makes the header a masthead rather than just large text.
 */
export function PageHeader({
  kicker,
  title,
  subtitle,
  actions,
}: {
  kicker?: string
  title: string
  subtitle?: ReactNode
  actions?: ReactNode
}) {
  return (
    <div className="mb-7 flex items-end justify-between gap-6 border-b border-border pb-4">
      <div className="min-w-0">
        {kicker && <div className="eyebrow mb-2 text-primary">{kicker}</div>}
        <h1 className="font-serif text-5xl leading-[0.95] text-foreground">{title}</h1>
        {subtitle && (
          <p className="mt-2.5 max-w-2xl text-sm text-muted-foreground">{subtitle}</p>
        )}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2 pb-1">{actions}</div>}
    </div>
  )
}

/** A section head inside a page — same mono voice as the column heads below it. */
export function SectionHead({ children }: { children: ReactNode }) {
  return <h2 className="eyebrow mb-3 text-muted-foreground">{children}</h2>
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <div className="border border-dashed border-border bg-card/50 p-12 text-center font-mono text-xs text-muted-foreground">
      {children}
    </div>
  )
}

export function ErrorNote({ message }: { message: string }) {
  return (
    <div className="mb-5 border border-border bg-bad/15 px-4 py-3 font-mono text-xs text-bad shadow-hard-sm">
      {message}
    </div>
  )
}
