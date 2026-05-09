"use client";

/**
 * Phase 4 W3 C3 iter 9 — Canvas artifact fallback panel.
 *
 * Shown when `POST /canvas/render` returns a 4xx with the
 * `error: "render_failed"` shape from `routes/canvas.rs`:
 *
 * ```json
 * { "error": "render_failed",
 *   "code": "timeout" | "adapter_error" | "body_too_large" |
 *           "unknown_kind" | "unimplemented",
 *   "artifact_kind": "mermaid",
 *   "message": "renderer timed out after 5000 ms (kind=Mermaid)" }
 * ```
 *
 * Shape matches the design's `canvas-artifact-error.tsx` brief: dashed
 * Tidepool glass panel with `lucide:triangle-alert` and the error code,
 * with the producer's raw source as a collapsed `<details>` so
 * operators can copy-paste it into a separate viewer.
 */

import * as React from "react";
import { TriangleAlert } from "lucide-react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";

export type CanvasArtifactErrorCode =
  | "timeout"
  | "adapter_error"
  | "body_too_large"
  | "unknown_kind"
  | "unimplemented"
  | "network"
  | "generic";

export interface CanvasArtifactErrorProps {
  /** Error code returned by the gateway. */
  code: CanvasArtifactErrorCode;
  /** Human-readable error message from the gateway. */
  message: string;
  /** Producer-supplied artifact kind, when known. */
  artifactKind?: "code" | "table" | "latex" | "sparkline" | "mermaid";
  /** Raw producer body — surfaced under a `<details>` so operators
   *  can recover the source even when the renderer fails. */
  rawSource?: string;
  /** Optional retry handler. Hidden when `undefined`. */
  onRetry?: () => void;
  className?: string;
}

export function CanvasArtifactError({
  code,
  message,
  artifactKind,
  rawSource,
  onRetry,
  className,
}: CanvasArtifactErrorProps) {
  const { t } = useTranslation();
  const headline = headlineForCode(code, t);

  return (
    <div
      role="alert"
      aria-live="polite"
      data-error-code={code}
      data-render-kind={artifactKind ?? "unknown"}
      className={cn(
        "cn-canvas-artifact-error my-3 rounded-lg border border-dashed border-tp-amber-soft bg-tp-glass-inner p-3 text-sm",
        className,
      )}
    >
      <div className="flex items-start gap-2">
        <TriangleAlert
          className="mt-0.5 h-4 w-4 flex-shrink-0 text-tp-amber"
          aria-hidden="true"
        />
        <div className="min-w-0 flex-1">
          <div className="font-medium text-foreground">
            {t("canvas.artifact.errorTitle")}
          </div>
          <div className="mt-0.5 text-xs text-muted-foreground">
            <span className="font-medium">{headline}</span>
            <span className="mx-1.5 opacity-60">·</span>
            <span className="break-all">{message}</span>
          </div>
        </div>
        {onRetry ? (
          <button
            type="button"
            onClick={onRetry}
            className="text-xs font-medium text-tp-amber hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber"
          >
            {t("canvas.artifact.errorRetry")}
          </button>
        ) : null}
      </div>
      {rawSource ? (
        <details className="mt-2">
          <summary className="cursor-pointer text-xs text-muted-foreground hover:text-foreground">
            {t("canvas.artifact.errorShowSource")}
          </summary>
          <pre className="mt-1.5 max-h-48 overflow-auto rounded bg-tp-glass-edge/40 p-2 text-xs font-mono whitespace-pre-wrap break-all">
            {rawSource}
          </pre>
        </details>
      ) : null}
    </div>
  );
}

function headlineForCode(
  code: CanvasArtifactErrorCode,
  t: (key: string) => string,
): string {
  switch (code) {
    case "timeout":
      return t("canvas.artifact.errorTimeout");
    case "adapter_error":
      return t("canvas.artifact.errorAdapter");
    case "body_too_large":
      return t("canvas.artifact.errorBodyTooLarge");
    case "unknown_kind":
      return t("canvas.artifact.errorUnknownKind");
    case "unimplemented":
    case "network":
    case "generic":
    default:
      return t("canvas.artifact.errorGeneric");
  }
}

export default CanvasArtifactError;
