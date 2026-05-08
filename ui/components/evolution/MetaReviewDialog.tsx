"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import { AlertTriangle, ShieldAlert, Sparkles } from "lucide-react";

import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import { CorlinmanApiError } from "@/lib/api";
import { cn } from "@/lib/utils";
import type { EvolutionProposal } from "@/lib/api";
import { isMetaKind } from "./types";

/**
 * Meta proposal review dialog — Phase 4 W2 B1 iter 6+7.
 *
 * Renders the per-kind diff for the four meta `EvolutionKind`s
 * (engine_config, engine_prompt, observer_filter, cluster_threshold) and
 * gates the Apply action behind a kind-aware confirmation flow:
 *
 *   - `engine_prompt` → two-step confirm. Step 1 demands the operator
 *     types the full proposal id (Apply stays disabled until exact
 *     match). Step 2 says "irreversible, manual rollback only" before
 *     firing the mutation. This is the highest blast-radius meta kind.
 *   - All other meta kinds → single confirm dialog with generic copy
 *     ("engine modifies engine — continue?"); no id-typing required.
 *
 * 403 handling: when the gateway returns
 * `{"error": "meta_approver_required", "user", "kind"}` (HTTP 403), this
 * surface renders an inline help block telling the operator to add
 * themselves to `[admin].meta_approver_users` in config.toml. Inline,
 * not toasted — operators need to read it without losing the dialog.
 */

export interface MetaReviewDialogProps {
  open: boolean;
  proposal: EvolutionProposal | null;
  onOpenChange: (open: boolean) => void;
  onApply: (id: string) => Promise<unknown>;
  /** Surfaced for parent observability — fired when an apply succeeds. */
  onApplied?: (id: string) => void;
}

export function MetaReviewDialog({
  open,
  proposal,
  onOpenChange,
  onApply,
  onApplied,
}: MetaReviewDialogProps) {
  const { t } = useTranslation();

  const [confirmStep, setConfirmStep] = React.useState<
    "review" | "step1" | "step2" | "generic"
  >("review");
  const [typedId, setTypedId] = React.useState("");
  const [submitting, setSubmitting] = React.useState(false);
  const [forbidden, setForbidden] = React.useState<{
    user: string;
    kind: string;
  } | null>(null);
  const [genericError, setGenericError] = React.useState<string | null>(null);

  // Reset transient state every time a new proposal is opened.
  React.useEffect(() => {
    if (open) {
      setConfirmStep("review");
      setTypedId("");
      setSubmitting(false);
      setForbidden(null);
      setGenericError(null);
    }
  }, [open, proposal?.id]);

  if (!proposal || !isMetaKind(proposal.kind)) {
    return null;
  }

  const isPrompt = proposal.kind === "engine_prompt";
  const idMatches = typedId === proposal.id;

  const startApply = () => {
    setForbidden(null);
    setGenericError(null);
    if (isPrompt) {
      setConfirmStep("step1");
    } else {
      setConfirmStep("generic");
    }
  };

  const advancePromptStep = () => {
    if (!idMatches) return;
    setConfirmStep("step2");
  };

  const fireApply = async () => {
    setSubmitting(true);
    setForbidden(null);
    setGenericError(null);
    try {
      await onApply(proposal.id);
      onApplied?.(proposal.id);
      onOpenChange(false);
    } catch (err) {
      const parsed = parseMetaApproverError(err);
      if (parsed) {
        setForbidden(parsed);
      } else {
        setGenericError(
          err instanceof Error ? err.message : String(err),
        );
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="max-w-2xl"
        // The DialogContent's default `description` Radix warning is
        // silenced by always rendering DialogDescription below.
      >
        <DialogHeader>
          <div className="flex items-center gap-2">
            <Sparkles className="h-4 w-4 text-tp-amber" aria-hidden />
            <DialogTitle>{t("evolution.tp.metaDialogTitle")}</DialogTitle>
            <span
              className={cn(
                "rounded-full border px-2 py-[2px] font-mono text-[10.5px]",
                "border-tp-amber/40 bg-tp-amber-soft text-tp-amber",
                "uppercase tracking-[0.08em]",
              )}
            >
              {t("evolution.tp.metaSelfImprovement")}
            </span>
          </div>
          <DialogDescription className="font-mono text-[11.5px] text-tp-ink-3">
            <span>{proposal.kind}</span>
            <span className="px-1.5 text-tp-ink-4">·</span>
            <span className="break-all">{proposal.target}</span>
            <span className="px-1.5 text-tp-ink-4">·</span>
            <span>#{proposal.id}</span>
          </DialogDescription>
        </DialogHeader>

        {forbidden ? (
          <ApproverRequiredHelp user={forbidden.user} />
        ) : null}

        {confirmStep === "review" ? (
          <div className="flex flex-col gap-3 py-1">
            <p className="text-[12.5px] leading-[1.6] text-tp-ink-2">
              {proposal.reasoning}
            </p>
            <MetaDiff proposal={proposal} />
          </div>
        ) : null}

        {confirmStep === "generic" ? (
          <div className="flex flex-col gap-3 py-1">
            <div
              className={cn(
                "flex items-start gap-2 rounded-lg border px-3 py-2.5",
                "border-tp-amber/30 bg-tp-amber-soft text-tp-ink",
              )}
            >
              <AlertTriangle
                className="mt-0.5 h-4 w-4 shrink-0 text-tp-amber"
                aria-hidden
              />
              <div className="flex flex-col gap-0.5">
                <span className="text-[12.5px] font-medium">
                  {t("evolution.tp.metaConfirmGenericTitle")}
                </span>
                <span className="text-[12px] text-tp-ink-2">
                  {t("evolution.tp.metaConfirmGenericBody")}
                </span>
              </div>
            </div>
          </div>
        ) : null}

        {confirmStep === "step1" ? (
          <div className="flex flex-col gap-3 py-1">
            <div
              className={cn(
                "flex items-start gap-2 rounded-lg border px-3 py-2.5",
                "border-tp-err/30 bg-tp-err-soft text-tp-ink",
              )}
            >
              <ShieldAlert
                className="mt-0.5 h-4 w-4 shrink-0 text-tp-err"
                aria-hidden
              />
              <div className="flex flex-col gap-0.5">
                <span className="text-[12.5px] font-medium">
                  {t("evolution.tp.metaPromptStep1Title")}
                </span>
                <span className="text-[12px] text-tp-ink-2">
                  {t("evolution.tp.metaPromptStep1Body")}
                </span>
              </div>
            </div>
            <label className="flex flex-col gap-1.5">
              <span className="font-mono text-[10.5px] uppercase tracking-[0.08em] text-tp-ink-4">
                {t("evolution.tp.metaPromptIdLabel")}
              </span>
              <input
                type="text"
                value={typedId}
                autoFocus
                onChange={(e) => setTypedId(e.target.value)}
                placeholder={t("evolution.tp.metaPromptIdPlaceholder")}
                aria-label={t("evolution.tp.metaPromptIdLabel")}
                className={cn(
                  "rounded-md border px-3 py-2 font-mono text-[12.5px]",
                  "border-tp-glass-edge bg-tp-glass-inner text-tp-ink",
                  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/55",
                )}
              />
              <span className="font-mono text-[11px] text-tp-ink-3">
                #{proposal.id}
              </span>
            </label>
          </div>
        ) : null}

        {confirmStep === "step2" ? (
          <div className="flex flex-col gap-3 py-1">
            <div
              className={cn(
                "flex items-start gap-2 rounded-lg border px-3 py-2.5",
                "border-tp-err/40 bg-tp-err-soft text-tp-ink",
              )}
            >
              <ShieldAlert
                className="mt-0.5 h-4 w-4 shrink-0 text-tp-err"
                aria-hidden
              />
              <div className="flex flex-col gap-0.5">
                <span className="text-[12.5px] font-medium">
                  {t("evolution.tp.metaPromptStep2Title")}
                </span>
                <span className="text-[12px] text-tp-ink-2">
                  {t("evolution.tp.metaPromptStep2Body")}
                </span>
              </div>
            </div>
          </div>
        ) : null}

        {genericError ? (
          <div
            role="alert"
            className={cn(
              "rounded-md border px-3 py-2 text-[12px]",
              "border-tp-err/40 bg-tp-err-soft text-tp-err",
            )}
          >
            {genericError}
          </div>
        ) : null}

        <DialogFooter>
          {confirmStep === "review" ? (
            <>
              <button
                type="button"
                onClick={() => onOpenChange(false)}
                className={cn(
                  "rounded-md px-3 py-1.5 text-[12.5px] font-medium",
                  "bg-tp-glass-inner text-tp-ink-2 hover:bg-tp-glass-inner-hover",
                  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/40",
                )}
              >
                {t("evolution.tp.metaDialogClose")}
              </button>
              <button
                type="button"
                onClick={startApply}
                aria-label={t("evolution.tp.metaApply")}
                className={cn(
                  "rounded-md px-3 py-1.5 text-[12.5px] font-medium",
                  "bg-tp-amber text-[#1a120d] shadow-tp-primary",
                  "hover:-translate-y-[1px] active:translate-y-0",
                  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/55",
                )}
              >
                {t("evolution.tp.metaApply")}
              </button>
            </>
          ) : null}

          {confirmStep === "generic" ? (
            <>
              <button
                type="button"
                onClick={() => setConfirmStep("review")}
                disabled={submitting}
                className={cn(
                  "rounded-md px-3 py-1.5 text-[12.5px] font-medium",
                  "bg-tp-glass-inner text-tp-ink-2 hover:bg-tp-glass-inner-hover",
                  "disabled:pointer-events-none disabled:opacity-50",
                )}
              >
                {t("evolution.tp.metaConfirmCancel")}
              </button>
              <button
                type="button"
                onClick={fireApply}
                disabled={submitting}
                aria-label={t("evolution.tp.metaConfirmContinue")}
                className={cn(
                  "rounded-md px-3 py-1.5 text-[12.5px] font-medium",
                  "bg-tp-amber text-[#1a120d] shadow-tp-primary",
                  "disabled:pointer-events-none disabled:opacity-50",
                )}
              >
                {t("evolution.tp.metaConfirmContinue")}
              </button>
            </>
          ) : null}

          {confirmStep === "step1" ? (
            <>
              <button
                type="button"
                onClick={() => setConfirmStep("review")}
                disabled={submitting}
                className={cn(
                  "rounded-md px-3 py-1.5 text-[12.5px] font-medium",
                  "bg-tp-glass-inner text-tp-ink-2 hover:bg-tp-glass-inner-hover",
                )}
              >
                {t("evolution.tp.metaConfirmCancel")}
              </button>
              <button
                type="button"
                onClick={advancePromptStep}
                disabled={!idMatches || submitting}
                aria-label={t("evolution.tp.metaApply")}
                className={cn(
                  "rounded-md px-3 py-1.5 text-[12.5px] font-medium",
                  "bg-tp-amber text-[#1a120d] shadow-tp-primary",
                  "disabled:pointer-events-none disabled:opacity-50",
                )}
              >
                {t("evolution.tp.metaApply")}
              </button>
            </>
          ) : null}

          {confirmStep === "step2" ? (
            <>
              <button
                type="button"
                onClick={() => setConfirmStep("step1")}
                disabled={submitting}
                className={cn(
                  "rounded-md px-3 py-1.5 text-[12.5px] font-medium",
                  "bg-tp-glass-inner text-tp-ink-2 hover:bg-tp-glass-inner-hover",
                )}
              >
                {t("evolution.tp.metaConfirmCancel")}
              </button>
              <button
                type="button"
                onClick={fireApply}
                disabled={submitting}
                aria-label={t("evolution.tp.metaPromptStep2Confirm")}
                className={cn(
                  "rounded-md px-3 py-1.5 text-[12.5px] font-medium",
                  "bg-tp-err text-white",
                  "disabled:pointer-events-none disabled:opacity-50",
                )}
              >
                {t("evolution.tp.metaPromptStep2Confirm")}
              </button>
            </>
          ) : null}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/**
 * Inline help block for the 403 `meta_approver_required` error envelope.
 * Renders inside the dialog body so the operator sees the actionable
 * config-edit instruction without losing the proposal context to a
 * dismissed toast.
 */
function ApproverRequiredHelp({ user }: { user: string }) {
  const { t } = useTranslation();
  return (
    <div
      role="alert"
      data-testid="meta-approver-required"
      className={cn(
        "flex items-start gap-2 rounded-lg border px-3 py-2.5",
        "border-tp-err/40 bg-tp-err-soft text-tp-ink",
      )}
    >
      <ShieldAlert
        className="mt-0.5 h-4 w-4 shrink-0 text-tp-err"
        aria-hidden
      />
      <span className="text-[12.5px] leading-[1.5] text-tp-ink-2">
        {t("evolution.tp.metaApproverRequired", { user })}
      </span>
    </div>
  );
}

/**
 * Per-kind diff renderer. The proposal's `diff` field is a serialized
 * `meta::*Payload` JSON string — we parse, fall back to a plain JSON
 * dump on parse failure rather than crashing the dialog.
 */
function MetaDiff({ proposal }: { proposal: EvolutionProposal }) {
  const { t } = useTranslation();
  const payload = parsePayload(proposal.diff);

  if (!payload) {
    return (
      <pre
        className={cn(
          "max-h-[260px] overflow-auto rounded-md border px-3 py-2",
          "border-tp-glass-edge bg-tp-glass-inner-strong",
          "font-mono text-[11.5px] leading-[1.55] text-tp-ink-2",
        )}
      >
        {proposal.diff || "(empty diff)"}
      </pre>
    );
  }

  if (proposal.kind === "engine_prompt") {
    const prev = pickString(payload, "previous_text");
    const next = pickString(payload, "proposed_text");
    return (
      <div
        className="grid gap-2 md:grid-cols-2"
        data-testid="meta-diff-engine-prompt"
      >
        <DiffPane
          label={t("evolution.tp.metaDiffPrevious")}
          tone="prev"
          body={prev}
        />
        <DiffPane
          label={t("evolution.tp.metaDiffProposed")}
          tone="next"
          body={next}
        />
      </div>
    );
  }

  if (proposal.kind === "engine_config") {
    const prev = stringifyJson(payload?.previous_value);
    const next = stringifyJson(payload?.proposed_value);
    return (
      <div
        className="grid gap-2 md:grid-cols-2"
        data-testid="meta-diff-engine-config"
      >
        <DiffPane
          label={`${t("evolution.tp.metaDiffPrevious")} · ${pickString(payload, "config_path")}`}
          tone="prev"
          body={prev}
        />
        <DiffPane
          label={`${t("evolution.tp.metaDiffProposed")} · ${pickString(payload, "config_path")}`}
          tone="next"
          body={next}
        />
      </div>
    );
  }

  if (proposal.kind === "observer_filter") {
    const prev = stringifyJson(payload?.previous_filter);
    const next = stringifyJson(payload?.proposed_filter);
    return (
      <div
        className="grid gap-2 md:grid-cols-2"
        data-testid="meta-diff-observer-filter"
      >
        <DiffPane
          label={`${t("evolution.tp.metaDiffPrevious")} · ${pickString(payload, "event_kind_pattern")}`}
          tone="prev"
          body={prev}
        />
        <DiffPane
          label={`${t("evolution.tp.metaDiffProposed")} · ${pickString(payload, "event_kind_pattern")}`}
          tone="next"
          body={next}
        />
      </div>
    );
  }

  // cluster_threshold — both are scalar floats; render as side-by-side.
  const prev = stringifyJson(payload?.previous_value);
  const next = stringifyJson(payload?.proposed_value);
  return (
    <div
      className="grid gap-2 md:grid-cols-2"
      data-testid="meta-diff-cluster-threshold"
    >
      <DiffPane
        label={`${t("evolution.tp.metaDiffPrevious")} · ${pickString(payload, "threshold_name")}`}
        tone="prev"
        body={prev}
      />
      <DiffPane
        label={`${t("evolution.tp.metaDiffProposed")} · ${pickString(payload, "threshold_name")}`}
        tone="next"
        body={next}
      />
    </div>
  );
}

function DiffPane({
  label,
  body,
  tone,
}: {
  label: string;
  body: string;
  tone: "prev" | "next";
}) {
  const toneCls =
    tone === "prev"
      ? "border-tp-err/30 bg-tp-err-soft/50"
      : "border-tp-ok/30 bg-tp-ok-soft/50";
  return (
    <div className={cn("rounded-md border", toneCls)}>
      <div className="border-b border-tp-glass-edge/50 px-2.5 py-1 font-mono text-[10.5px] uppercase tracking-[0.08em] text-tp-ink-3">
        {label}
      </div>
      <pre
        className={cn(
          "max-h-[260px] overflow-auto px-2.5 py-2",
          "font-mono text-[11.5px] leading-[1.55] text-tp-ink",
          "whitespace-pre-wrap break-words",
        )}
      >
        {body || "—"}
      </pre>
    </div>
  );
}

function parsePayload(raw: string): Record<string, unknown> | null {
  if (!raw) return null;
  try {
    const v = JSON.parse(raw);
    if (v && typeof v === "object" && !Array.isArray(v)) {
      return v as Record<string, unknown>;
    }
    return null;
  } catch {
    return null;
  }
}

function pickString(obj: Record<string, unknown> | null, key: string): string {
  const v = obj?.[key];
  return typeof v === "string" ? v : "";
}

function stringifyJson(v: unknown): string {
  if (v === undefined || v === null) return "";
  if (typeof v === "string") return v;
  try {
    return JSON.stringify(v, null, 2);
  } catch {
    return String(v);
  }
}

/**
 * Detect the gateway's 403 envelope:
 *
 *   { "error": "meta_approver_required", "user": "...", "kind": "..." }
 *
 * `apiFetch` throws a `CorlinmanApiError` whose `message` is the raw
 * response body (JSON string). We only treat it as the meta-approver
 * 403 when the status is 403 *and* the body parses to that envelope —
 * other 403s (auth, etc.) fall through to the generic error surface.
 */
export function parseMetaApproverError(
  err: unknown,
): { user: string; kind: string } | null {
  if (!(err instanceof CorlinmanApiError)) return null;
  if (err.status !== 403) return null;
  try {
    const body = JSON.parse(err.message);
    if (
      body &&
      typeof body === "object" &&
      body.error === "meta_approver_required" &&
      typeof body.user === "string" &&
      typeof body.kind === "string"
    ) {
      return { user: body.user, kind: body.kind };
    }
  } catch {
    // not JSON — ignore
  }
  return null;
}

export default MetaReviewDialog;
