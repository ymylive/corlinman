"use client";

/**
 * Phase 4 W3 C3 iter 9 — Canvas artifact viewer.
 *
 * Consumes the rendered output of the gateway's iter-8
 * `POST /canvas/render` endpoint:
 *
 * ```ts
 * {
 *   html_fragment: string;
 *   theme_class: "tp-light" | "tp-dark";
 *   content_hash: string;       // 64-char blake3 hex
 *   render_kind: "code" | "table" | "latex" | "sparkline" | "mermaid";
 *   warnings: string[];
 * }
 * ```
 *
 * Rendering strategy:
 *   - Wrap the fragment in `<figure role="figure">` with an aria-label
 *     keyed off `render_kind` so screen readers identify the artifact.
 *   - Drop the `theme_class` (e.g. `tp-light`) onto the wrapper so
 *     non-CSS-var consumers (Swift, future static export) match the
 *     light/dark token palette already in `ui/app/globals.css`.
 *   - Belt-and-braces sanitisation via [`stripUnsafeMarkup`]: the
 *     server is the primary line of defence (`ammonia` whitelist on the
 *     mermaid path, syntect's class-only emission on `code`, etc.), but
 *     we strip `<script>` blocks and `on*=` attributes here too so a
 *     producer regression can't escalate to XSS.
 *   - `dangerouslySetInnerHTML` is unavoidable; React has no other path
 *     to inject server-rendered HTML. The strip is the trade-off.
 *
 * The component is consciously dumb: no fetcher, no retry, no SSE
 * subscription. Iter 10 (E2E) wires the producer-skill flow that drives
 * it; today the SessionViewer / playground feed it directly.
 */

import * as React from "react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";

/** Wire shape of the gateway's `RenderedArtifact` JSON. */
export interface RenderedArtifact {
  html_fragment: string;
  theme_class: "tp-light" | "tp-dark";
  content_hash: string;
  render_kind: "code" | "table" | "latex" | "sparkline" | "mermaid";
  warnings: string[];
}

export interface CanvasArtifactProps {
  artifact: RenderedArtifact;
  /** Optional extra classes on the outer `<figure>`. */
  className?: string;
  /**
   * If `true`, the human-friendly kind label and content-hash chip are
   * hidden — useful when embedding inside a transcript bubble that
   * already labels the artifact externally.
   */
  hideMeta?: boolean;
}

/**
 * Drop-in renderer for a single rendered artifact.
 *
 * The HTML fragment lives behind `<figure>` so the surrounding
 * transcript can target the figure for hover / focus styling without
 * leaking into the artifact's own DOM.
 */
export const CanvasArtifact = React.forwardRef<HTMLElement, CanvasArtifactProps>(
  function CanvasArtifact({ artifact, className, hideMeta = false }, ref) {
    const { t } = useTranslation();
    const sanitized = React.useMemo(
      () => stripUnsafeMarkup(artifact.html_fragment),
      [artifact.html_fragment],
    );
    const kindLabel = kindToLabel(artifact.render_kind, t);
    const shortHash = artifact.content_hash
      ? `${artifact.content_hash.slice(0, 8)}…`
      : "";

    return (
      <figure
        ref={ref}
        role="figure"
        aria-label={t("canvas.artifact.figureLabel")}
        data-render-kind={artifact.render_kind}
        data-content-hash={artifact.content_hash || undefined}
        className={cn(
          "cn-canvas-artifact group relative my-3 overflow-hidden rounded-lg border border-tp-glass-edge bg-tp-glass-inner",
          // The gateway also returns `theme_class`; we mirror it as a
          // CSS class so non-CSS-var consumers can target it. The
          // browser ignores it when the global theme is in control.
          artifact.theme_class,
          className,
        )}
      >
        {hideMeta ? null : (
          <header className="flex items-center justify-between px-3 py-1.5 text-[11px] uppercase tracking-wide text-muted-foreground border-b border-tp-glass-edge/60">
            <span aria-hidden="true" className="font-medium">
              {kindLabel}
            </span>
            {shortHash ? (
              <span
                title={t("canvas.artifact.hashTitle", { hash: artifact.content_hash })}
                className="font-mono tabular-nums opacity-70"
              >
                {shortHash}
              </span>
            ) : null}
          </header>
        )}
        <div
          // Inline class lives on globals.css `cn-canvas-*` tokens; the
          // server also emits matching classes inside `html_fragment`.
          className="cn-canvas-artifact-body p-3 text-sm leading-relaxed"
          dangerouslySetInnerHTML={{ __html: sanitized }}
        />
        {artifact.warnings.length > 0 ? (
          <figcaption className="border-t border-tp-glass-edge/60 px-3 py-1.5 text-xs text-muted-foreground">
            <span className="mr-2 font-medium uppercase tracking-wide opacity-70">
              {t("canvas.artifact.warningsLabel")}
            </span>
            <span>{artifact.warnings.join(" · ")}</span>
          </figcaption>
        ) : null}
      </figure>
    );
  },
);

CanvasArtifact.displayName = "CanvasArtifact";

export default CanvasArtifact;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function kindToLabel(
  kind: RenderedArtifact["render_kind"],
  t: (key: string) => string,
): string {
  switch (kind) {
    case "code":
      return t("canvas.artifact.kindCode");
    case "table":
      return t("canvas.artifact.kindTable");
    case "latex":
      return t("canvas.artifact.kindLatex");
    case "sparkline":
      return t("canvas.artifact.kindSparkline");
    case "mermaid":
      return t("canvas.artifact.kindMermaid");
    default:
      return kind;
  }
}

/**
 * Defensive client-side strip. The server already sanitises (`ammonia`
 * tag-allowlist + class-only emission), but a regression in any
 * adapter shouldn't be able to introduce XSS. Two axes:
 *
 *  1. `<script>` and `<style>` blocks — drop the entire element.
 *  2. `on…="…"` event-handler attributes — strip the attribute pair.
 *
 * This is *belt and braces*. Anything ambiguous returns the original
 * string (we're complementing the server, not replacing it). For
 * production-grade isolation a follow-up iteration can add DOMPurify
 * — out of C3 iter 9 scope.
 */
export function stripUnsafeMarkup(html: string): string {
  let out = html;
  // Remove <script>…</script> and <style>…</style> blocks. The flag `gis`
  // is broadly supported in modern browsers + jsdom.
  out = out.replace(/<\s*(script|style)\b[^>]*>[\s\S]*?<\s*\/\s*\1\s*>/gi, "");
  // Strip remaining empty `<script` or `<style` open tags (defensive
  // against malformed input that broke the previous regex).
  out = out.replace(/<\s*\/?\s*(script|style)\b[^>]*>/gi, "");
  // Drop `on…="…"` and `on…='…'` attributes. Whitespace-prefixed so we
  // don't eat the attribute name's leading separator.
  out = out.replace(/\s+on[a-z]+\s*=\s*"[^"]*"/gi, "");
  out = out.replace(/\s+on[a-z]+\s*=\s*'[^']*'/gi, "");
  // Drop `javascript:` URIs in href / src.
  out = out.replace(/(href|src)\s*=\s*"javascript:[^"]*"/gi, "$1=\"#\"");
  out = out.replace(/(href|src)\s*=\s*'javascript:[^']*'/gi, "$1='#'");
  return out;
}
