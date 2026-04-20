import * as React from "react";
import { cn } from "@/lib/utils";

/** Minimal styled checkbox. Native `input[type=checkbox]` + Tailwind.
 *
 * We intentionally avoid `@radix-ui/react-checkbox` — the approvals page
 * is the only consumer right now and the batch-select UX doesn't need
 * indeterminate state animations. ~15 lines is worth less than a new dep.
 */
export interface CheckboxProps
  extends Omit<React.InputHTMLAttributes<HTMLInputElement>, "type"> {
  "aria-label": string;
}

export const Checkbox = React.forwardRef<HTMLInputElement, CheckboxProps>(
  ({ className, ...props }, ref) => (
    <input
      ref={ref}
      type="checkbox"
      className={cn(
        "h-4 w-4 cursor-pointer rounded border-input bg-background accent-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50",
        className,
      )}
      {...props}
    />
  ),
);
Checkbox.displayName = "Checkbox";
