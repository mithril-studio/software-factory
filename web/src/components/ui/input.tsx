import * as React from "react"
import { cn } from "@/lib/utils"

/** A text field. The first one in this app that is not part of the login form — which is why
 *  it exists as a primitive now rather than as a class string copied a second time. */
function Input({ className, ...props }: React.ComponentProps<"input">) {
  return (
    <input
      className={cn(
        "h-9 w-full rounded-md border border-border bg-transparent px-3 text-sm text-foreground",
        "outline-none transition-colors placeholder:text-muted-foreground",
        "focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50",
        className
      )}
      {...props}
    />
  )
}

export { Input }
