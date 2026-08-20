import * as React from "react"
import { cn } from "@/lib/utils"

/** A text field. The first one in this app that is not part of the login form — which is why
 *  it exists as a primitive now rather than as a class string copied a second time.
 *
 *  Square, black-framed and mono: what goes in these is a repo slug or a credential, never
 *  prose, and mono is the app's voice for anything the machine will read back. */
function Input({ className, ...props }: React.ComponentProps<"input">) {
  return (
    <input
      className={cn(
        "h-9 w-full border border-input bg-background px-3 font-mono text-sm text-foreground",
        "outline-none transition-colors placeholder:text-muted-foreground/70",
        "focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-card",
        "disabled:opacity-50",
        className
      )}
      {...props}
    />
  )
}

export { Input }
