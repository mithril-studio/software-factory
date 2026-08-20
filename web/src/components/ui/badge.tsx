import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"
import { cn } from "@/lib/utils"

/**
 * Square, mono, uppercase. The black hairline is what makes a tinted chip read as a
 * stamped object rather than as a highlighted word, so every variant keeps it.
 */
const badgeVariants = cva(
  "inline-flex items-center whitespace-nowrap border px-1.5 py-px font-mono text-[10px] font-medium uppercase tracking-wider leading-4",
  {
    variants: {
      variant: {
        default: "border-border bg-secondary text-secondary-foreground",
        outline: "border-subtle text-muted-foreground",
        ok: "border-border bg-ok/20 text-ok",
        warn: "border-border bg-warn/20 text-warn",
        bad: "border-border bg-bad/20 text-bad",
        muted: "border-subtle bg-muted text-muted-foreground",
        accent: "border-border bg-accent text-accent-foreground",
      },
    },
    defaultVariants: { variant: "default" },
  }
)

function Badge({
  className,
  variant,
  ...props
}: React.ComponentProps<"span"> & VariantProps<typeof badgeVariants>) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />
}

export { Badge, badgeVariants }
