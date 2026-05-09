"use client";

/**
 * Phase 4 W3 C3 iter 9 — Canvas artifact skeleton.
 *
 * Shown for the ~50ms between SSE arrival and the rendered HTML
 * landing in [`CanvasArtifact`]. Reuses the global `bg-state-skeleton`
 * token (defined in `ui/app/globals.css`) so the colour matches the
 * other admin-UI skeletons (request log, plugin detail, …).
 *
 * Two-line layout: a kind chip at the top, then a tall body block.
 * Heights are roughly tuned to match the average artifact body so
 * the page reflow when real HTML lands is visually small.
 */

import * as React from "react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";

export interface CanvasArtifactLoadingProps {
  className?: string;
  /**
   * Hint about the kind being rendered, used to pick a roughly-correct
   * skeleton height. Optional — defaults to a medium body.
   */
  kindHint?: "code" | "table" | "latex" | "sparkline" | "mermaid";
}

export function CanvasArtifactLoading({
  className,
  kindHint,
}: CanvasArtifactLoadingProps) {
  const { t } = useTranslation();
  const bodyHeight = bodyHeightForKind(kindHint);
  return (
    <div
      role="status"
      aria-live="polite"
      aria-label={t("canvas.artifact.loading")}
      className={cn(
        "cn-canvas-artifact-loading my-3 overflow-hidden rounded-lg border border-tp-glass-edge bg-tp-glass-inner animate-pulse",
        className,
      )}
      data-render-kind={kindHint ?? "unknown"}
    >
      <header className="flex items-center justify-between px-3 py-1.5">
        <div className="h-3 w-16 rounded bg-state-skeleton" />
        <div className="h-3 w-20 rounded bg-state-skeleton" />
      </header>
      <div className="px-3 pb-3">
        <div
          className="rounded bg-state-skeleton"
          style={{ height: bodyHeight }}
          aria-hidden="true"
        />
      </div>
      <span className="sr-only">{t("canvas.artifact.loading")}</span>
    </div>
  );
}

function bodyHeightForKind(
  kind: CanvasArtifactLoadingProps["kindHint"],
): number {
  switch (kind) {
    case "sparkline":
      return 32;
    case "latex":
      return 40;
    case "table":
      return 96;
    case "code":
      return 120;
    case "mermaid":
      return 160;
    default:
      return 80;
  }
}

export default CanvasArtifactLoading;
