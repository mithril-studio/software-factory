import * as React from "react"
import { Slot } from "@radix-ui/react-slot"
import { cva, type VariantProps } from "class-variance-authority"
import { cn } from "@/lib/utils"

/**
 * A key, not a pill. Square, black-framed, sitting on its own 2px shadow; hover lifts
 * it a pixel and deepens the shadow, press seats it flat. The lift lives on the
 * variants rather than the base so `ghost` can genuinely have no shadow — a tertiary
 * control that moves under the cursor reads as more important than it is.
 */
const LIFT = "shadow-hard-sm hard-lift-sm"

const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap border font-mono text-[11px] font-medium uppercase tracking-wider focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background disabled:pointer-events-none disabled:opacity-50 [&_svg]:size-3.5 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        default: `border-border bg-primary text-primary-foreground ${LIFT}`,
        secondary: `border-border bg-secondary text-secondary-foreground ${LIFT}`,
        outline: `border-border bg-card text-foreground hover:bg-muted ${LIFT}`,
        ghost:
          "border-transparent bg-transparent text-muted-foreground transition-colors hover:bg-muted hover:text-foreground",
        destructive: `border-border bg-destructive text-white ${LIFT}`,
      },
      size: {
        default: "h-9 px-4 py-2",
        sm: "h-8 px-3 text-[10px]",
        icon: "size-9",
      },
    },
    defaultVariants: { variant: "default", size: "default" },
  }
)

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean
}

function Button({ className, variant, size, asChild = false, ...props }: ButtonProps) {
  const Comp = asChild ? Slot : "button"
  return <Comp className={cn(buttonVariants({ variant, size, className }))} {...props} />
}

export { Button, buttonVariants }
