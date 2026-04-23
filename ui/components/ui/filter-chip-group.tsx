"use client";

import * as React from "react";
import { cn } from "@/lib/utils";

/**
 * Filter tab-group — pill-style chips with optional severity dot and count.
 *
 * Controlled component: caller owns the `value` and the `onChange`
 * callback. Single-select by default; `multi=true` switches to multi-select
 * (no exclusive toggle — use separate radio-group if exclusive semantics
 * are required).
 *
 * Used by:
 *   - Dashboard activity pane (ok / warn / err / info filter)
 *   - Logs page (severity + subsystem filters reuse the same chip language)
 *
 * Accessibility:
 *   - Wraps children in `role="tablist"` / `role="tab"` for single-select.
 *   - For `multi`, wraps in a `role="group"` with buttons carrying
 *     `aria-pressed`.
 */

export type FilterChipTone = "ok" | "warn" | "err" | "info" | "neutral";

export interface FilterChipOption {
  value: string;
  label: string;
  /** Optional numeric count shown next to the label. */
  count?: number;
  /** Severity tone. `neutral` (default) shows no dot. */
  tone?: FilterChipTone;
  /** Disables the chip for interaction. */
  disabled?: boolean;
}

interface BaseProps extends Omit<React.HTMLAttributes<HTMLDivElement>, "onChange"> {
  options: FilterChipOption[];
}

export type FilterChipGroupProps =
  | (BaseProps & {
      multi?: false;
      value: string;
      onChange: (next: string) => void;
      label?: string;
    })
  | (BaseProps & {
      multi: true;
      value: string[];
      onChange: (next: string[]) => void;
      label?: string;
    });

const toneDot: Record<FilterChipTone, string> = {
  ok: "bg-tp-ok",
  warn: "bg-tp-warn",
  err: "bg-tp-err",
  info: "bg-tp-ink-4",
  neutral: "",
};

export function FilterChipGroup(props: FilterChipGroupProps) {
  // Destructure every known prop so nothing leaks to the DOM. `multi`,
  // `value`, and `onChange` are specific to this component and must not
  // be spread onto the wrapping <div>.
  const isMulti = "multi" in props && props.multi === true;
  const {
    options,
    className,
    label,
    ...domRest
  }: BaseProps & { label?: string } = props;
  // Strip the discriminated-union-only keys from whatever's left.
  const {
    value: _v,
    onChange: _oc,
    multi: _m,
    ...rest
  } = domRest as BaseProps & {
    value: unknown;
    onChange: unknown;
    multi?: boolean;
  };
  void _v; void _oc; void _m;

  function isActive(v: string): boolean {
    if (isMulti) return (props.value as string[]).includes(v);
    return props.value === v;
  }

  function pick(v: string) {
    if (isMulti) {
      const cur = new Set<string>(props.value as string[]);
      if (cur.has(v)) cur.delete(v);
      else cur.add(v);
      (props.onChange as (n: string[]) => void)(Array.from(cur));
    } else {
      (props.onChange as (n: string) => void)(v);
    }
  }

  return (
    <div
      role={isMulti ? "group" : "tablist"}
      aria-label={label}
      className={cn("flex flex-wrap items-center gap-1.5", className)}
      {...(rest as React.HTMLAttributes<HTMLDivElement>)}
    >
      {options.map((opt) => {
        const active = isActive(opt.value);
        return (
          <button
            key={opt.value}
            type="button"
            role={isMulti ? undefined : "tab"}
            aria-pressed={isMulti ? active : undefined}
            aria-selected={isMulti ? undefined : active}
            disabled={opt.disabled}
            data-active={active || undefined}
            onClick={() => pick(opt.value)}
            className={cn(
              "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-[3px]",
              "font-mono text-[10.5px] tracking-wide",
              "transition-colors",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/50",
              active
                ? "bg-tp-ink text-tp-glass-hl border-tp-ink data-[active]:[color:var(--tp-glass)] dark:data-[active]:[color:var(--tp-ink)]"
                : "bg-tp-glass-inner text-tp-ink-3 border-tp-glass-edge hover:bg-tp-glass-inner-hover hover:text-tp-ink-2",
              opt.disabled && "cursor-not-allowed opacity-50",
            )}
          >
            {opt.tone && opt.tone !== "neutral" ? (
              <span
                aria-hidden
                className={cn("h-[5px] w-[5px] rounded-full", toneDot[opt.tone])}
              />
            ) : null}
            <span>{opt.label}</span>
            {typeof opt.count === "number" ? (
              <span className="tabular-nums text-current/60">{opt.count}</span>
            ) : null}
          </button>
        );
      })}
    </div>
  );
}

export default FilterChipGroup;
