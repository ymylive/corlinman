"use client";

import * as React from "react";
import { AnimatePresence, motion } from "framer-motion";

import { useMotion } from "@/components/ui/motion-safe";

/**
 * One-shot save-success ripple. Keys on `id` so each increment re-mounts and
 * re-plays the animation. Absolutely positioned; the parent is expected to
 * set `position: relative` and `overflow: visible`. Skipped entirely under
 * `prefers-reduced-motion`.
 *
 * Phase 5e retoken: the default colour is now the Tidepool amber token
 * (`var(--tp-amber)`), to match the primary save CTA.
 *
 * Timing: opacity 0.45 → 0, scale 0 → 6 over 600ms.
 */
export interface SuccessRippleProps {
  /**
   * Monotonic id. Each new value replays the ripple. A value of `0` (or
   * unchanged) renders nothing.
   */
  id: number;
  /** Ripple colour — defaults to the Tidepool amber token. */
  color?: string;
}

export function SuccessRipple({ id, color = "var(--tp-amber)" }: SuccessRippleProps) {
  const { reduced } = useMotion();
  if (reduced) return null;
  return (
    <AnimatePresence>
      {id > 0 ? (
        <motion.span
          key={id}
          aria-hidden="true"
          initial={{ opacity: 0.45, scale: 0 }}
          animate={{ opacity: 0, scale: 6 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.6, ease: "easeOut" }}
          className="pointer-events-none absolute inset-0 rounded-lg"
          style={{ backgroundColor: color }}
          data-testid="config-save-ripple"
        />
      ) : null}
    </AnimatePresence>
  );
}
