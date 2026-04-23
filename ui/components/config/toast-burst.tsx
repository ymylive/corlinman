"use client";

import * as React from "react";
import { motion } from "framer-motion";
import { CheckCircle } from "lucide-react";

import { useMotion } from "@/components/ui/motion-safe";

/**
 * Three-dot burst decoration for the config-save success toast. Dots shoot
 * outward from a common origin for 500ms, then fade. Purely decorative —
 * `aria-hidden` so screen readers announce only the toast text.
 *
 * Phase 5e retoken: the dots + static-fallback checkmark now use the
 * Tidepool amber token so the toast reads as part of the warm-glass
 * dialect rather than the legacy `--ok` green.
 *
 * Under `prefers-reduced-motion`, falls back to a static amber CheckCircle
 * so users still get a success signal without motion.
 */
const BURST_VECTORS: readonly [number, number][] = [
  [-14, -10],
  [14, -10],
  [0, 14],
];

export function ToastBurst() {
  const { reduced } = useMotion();

  if (reduced) {
    return (
      <span
        aria-hidden="true"
        className="inline-flex h-4 w-4 items-center justify-center text-tp-amber"
        data-testid="config-toast-check"
      >
        <CheckCircle className="h-4 w-4" />
      </span>
    );
  }

  return (
    <span
      aria-hidden="true"
      className="relative inline-block h-4 w-4"
      data-testid="config-toast-burst"
    >
      {BURST_VECTORS.map(([x, y], i) => (
        <motion.span
          key={i}
          className="absolute left-1/2 top-1/2 h-1.5 w-1.5 -translate-x-1/2 -translate-y-1/2 rounded-full bg-tp-amber"
          initial={{ opacity: 1, x: 0, y: 0, scale: 0.6 }}
          animate={{ opacity: 0, x, y, scale: 1 }}
          transition={{ duration: 0.5, ease: "easeOut", delay: i * 0.04 }}
        />
      ))}
    </span>
  );
}
