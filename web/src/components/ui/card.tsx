import * as React from "react"
import { cn } from "@/lib/utils"

/**
 * A slab: black hairline, square corners, solid offset shadow. `interactive` adds the
 * lift — reserve it for cards that are actually a link or a button, since a shadow
 * that moves under the cursor promises somewhere to go.
 */
function Card({
  className,
  interactive = false,
  ...props
}: React.ComponentProps<"div"> & { interactive?: boolean }) {
  return (
    <div
      className={cn(
        "border border-border bg-card text-card-foreground shadow-hard",
        interactive && "hard-lift",
        className
      )}
      {...props}
    />
  )
}

function CardHeader({ className, ...props }: React.ComponentProps<"div">) {
  return <div className={cn("flex flex-col gap-1.5 p-optical", className)} {...props} />
}

function CardTitle({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div className={cn("font-serif text-2xl leading-none text-foreground", className)} {...props} />
  )
}

function CardDescription({ className, ...props }: React.ComponentProps<"div">) {
  return <div className={cn("text-sm text-muted-foreground", className)} {...props} />
}

function CardContent({ className, ...props }: React.ComponentProps<"div">) {
  return <div className={cn("p-optical pt-0", className)} {...props} />
}

/** Uppercase mono section head, for a panel that leads with a label instead of a title. */
function CardEyebrow({ className, ...props }: React.ComponentProps<"div">) {
  return <div className={cn("eyebrow text-muted-foreground", className)} {...props} />
}

export { Card, CardHeader, CardTitle, CardDescription, CardContent, CardEyebrow }
