/**
 * Phase 4 W3 C3 iter 10 — present-frame SSE event parser.
 *
 * The gateway's `routes/canvas.rs::post_frame` enriches `present`
 * frames in-line: when the producer payload deserialises into a C3
 * `CanvasPresentPayload`, the renderer is invoked and its output is
 * attached under `payload.rendered` (success) or `payload.render_error`
 * (failure). On the SSE bus, subscribers see:
 *
 *  ```json
 *  // success
 *  { "event_id": "…", "kind": "present", "session_id": "cs_…",
 *    "payload": {
 *      "artifact_kind": "code", "body": {…}, "idempotency_key": "art_…",
 *      "rendered": { "html_fragment": "…", "theme_class": "tp-light",
 *                    "content_hash": "…", "render_kind": "code",
 *                    "warnings": [] }
 *    } }
 *
 *  // failure
 *  { "event_id": "…", "kind": "present", "session_id": "cs_…",
 *    "payload": {
 *      "artifact_kind": "mermaid", "body": {…}, "idempotency_key": "art_…",
 *      "render_error": { "code": "adapter_error",
 *                        "message": "…", "artifact_kind": "mermaid" }
 *    } }
 *  ```
 *
 * This module is the symmetric reader: it picks one of three branches
 * for the consumer (transcript-view, playground, future Swift web
 * shim) — `artifact`, `error`, or `passthrough` — without forcing
 * the consumer to know the JSON layout.
 *
 * Pure functions; safe for SSR / RSC.
 */
import type { RenderedArtifact } from "./canvas-artifact";
import type { CanvasArtifactErrorCode } from "./canvas-artifact-error";

/** Parsed `present`-frame outcome. Mutually exclusive variants. */
export type ParsedPresentFrame =
  | { kind: "artifact"; artifact: RenderedArtifact; idempotencyKey: string }
  | {
      kind: "error";
      code: CanvasArtifactErrorCode;
      message: string;
      artifactKind?: RenderedArtifact["render_kind"];
      idempotencyKey?: string;
    }
  | { kind: "passthrough"; payload: Record<string, unknown> };

/** Wire shape of a single canvas SSE event (what the gateway emits). */
export interface CanvasSseEvent {
  event_id?: string;
  session_id?: string;
  kind?: string;
  payload?: unknown;
  at_ms?: number;
}

/**
 * Parse one canvas SSE event into a typed `ParsedPresentFrame`.
 *
 * Returns `null` when the event isn't a `present` frame (UI consumers
 * forward those to the legacy a2ui handlers untouched).
 */
export function parsePresentFrame(evt: CanvasSseEvent): ParsedPresentFrame | null {
  if (evt.kind !== "present") return null;
  const payload = evt.payload as Record<string, unknown> | undefined;
  if (!payload || typeof payload !== "object") {
    return { kind: "passthrough", payload: {} };
  }

  const idempotencyKey =
    typeof payload.idempotency_key === "string" ? payload.idempotency_key : "";

  // Success — the gateway attached a `rendered` block.
  if (isRenderedArtifact(payload.rendered)) {
    return {
      kind: "artifact",
      artifact: payload.rendered,
      idempotencyKey,
    };
  }

  // Failure — the gateway attached a `render_error` block.
  if (isRenderError(payload.render_error)) {
    return {
      kind: "error",
      code: payload.render_error.code,
      message: payload.render_error.message,
      artifactKind: payload.render_error.artifact_kind,
      idempotencyKey: idempotencyKey || undefined,
    };
  }

  // Legacy `present` frame (pre-C3, a2ui-style). Pass through so the
  // consumer can keep its existing behaviour.
  return { kind: "passthrough", payload };
}

// ---------------------------------------------------------------------------
// Type guards
// ---------------------------------------------------------------------------

function isRenderedArtifact(v: unknown): v is RenderedArtifact {
  if (!v || typeof v !== "object") return false;
  const r = v as Record<string, unknown>;
  return (
    typeof r.html_fragment === "string" &&
    (r.theme_class === "tp-light" || r.theme_class === "tp-dark") &&
    typeof r.content_hash === "string" &&
    isRenderKind(r.render_kind) &&
    Array.isArray(r.warnings)
  );
}

function isRenderError(
  v: unknown,
): v is {
  code: CanvasArtifactErrorCode;
  message: string;
  artifact_kind?: RenderedArtifact["render_kind"];
} {
  if (!v || typeof v !== "object") return false;
  const r = v as Record<string, unknown>;
  return typeof r.code === "string" && typeof r.message === "string";
}

function isRenderKind(v: unknown): v is RenderedArtifact["render_kind"] {
  return (
    v === "code" ||
    v === "table" ||
    v === "latex" ||
    v === "sparkline" ||
    v === "mermaid"
  );
}
