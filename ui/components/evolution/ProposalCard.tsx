"use client";

import * as React from "react";
import { motion } from "framer-motion";
import { useTranslation } from "react-i18next";
import { ChevronDown, ChevronUp, Check, X } from "lucide-react";

import { GlassPanel } from "@/components/ui/glass-panel";
import { useMotion } from "@/components/ui/motion-safe";
import { cn } from "@/lib/utils";
import type { EvolutionProposal, EvolutionRisk } from "./types";

/**
 * One proposal in the pending queue, rendered as a soft glass panel that
 * lifts on hover. The card has two layers:
 *
 *   1. Always-visible head row — kind badge, risk badge, target path,
 *      one-line "X seconds ago · N signals · trace" meta, primary
 *      Approve / Deny actions.
 *   2. Expandable detail body — agent's reasoning prose plus the unified
 *      diff in a mono'd block.
 *
 * Deny is a two-step affordance: the first click swaps the action row for
 * an inline reason input (no modal). A second click on "Confirm deny"
 * dispatches the mutation; the optional reason is forwarded as-is.
 *
 * Spring-out on success: when `isDeparting` is true (parent flips it after
 * a successful mutation) the card scales/fades out via `springPop` reversed
 * before the parent removes it from the cache. Honours reduced-motion.
 */

const RISK_TONE: Record<
  EvolutionRisk,
  {
    border: string;
    bg: string;
    text: string;
    label: keyof EvolutionLabels;
  }
> = {
  low: {
    border: "border-tp-amber/25",
    bg: "bg-tp-amber-soft",
    text: "text-tp-amber",
    label: "riskLow",
  },
  medium: {
    border: "border-tp-ember/35",
    bg: "bg-[color-mix(in_oklch,var(--tp-ember)_14%,transparent)]",
    text: "text-tp-ember",
    label: "riskMedium",
  },
  high: {
    border: "border-tp-err/40",
    bg: "bg-tp-err-soft",
    text: "text-tp-err",
    label: "riskHigh",
  },
};

type EvolutionLabels = {
  riskLow: string;
  riskMedium: string;
  riskHigh: string;
};

export interface ProposalCardProps {
  proposal: EvolutionProposal;
  /** Tick (ms epoch) used to render "Xs ago". */
  now: number;
  onApprove: (id: string) => void;
  onDeny: (id: string, reason?: string) => void;
  disabled?: boolean;
  /** When true, the card animates out on next paint. */
  isDeparting?: boolean;
}

export function ProposalCard({
  proposal,
  now,
  onApprove,
  onDeny,
  disabled = false,
  isDeparting = false,
}: ProposalCardProps) {
  const { t } = useTranslation();
  const { reduced } = useMotion();
  const [expanded, setExpanded] = React.useState(false);
  const [denyOpen, setDenyOpen] = React.useState(false);
  const [denyReason, setDenyReason] = React.useState("");

  const risk = RISK_TONE[proposal.risk];
  const ageMs = Math.max(0, now - proposal.created_at);
  const ageLabel = formatAgeLabel(ageMs, t);

  const headingId = React.useId();

  const handleApprove = () => {
    if (disabled) return;
    onApprove(proposal.id);
  };

  const handleDenyClick = () => {
    if (disabled) return;
    setDenyOpen(true);
  };

  const handleDenyConfirm = () => {
    if (disabled) return;
    onDeny(proposal.id, denyReason.trim() || undefined);
    setDenyOpen(false);
    setDenyReason("");
  };

  const handleDenyCancel = () => {
    setDenyOpen(false);
    setDenyReason("");
  };

  return (
    <motion.div
      layout={reduced ? false : "position"}
      animate={
        isDeparting
          ? { opacity: 0, scale: 0.97, transition: { duration: 0.4 } }
          : { opacity: 1, scale: 1 }
      }
      initial={reduced ? false : { opacity: 0, y: 8 }}
    >
      <GlassPanel
        as="article"
        variant="soft"
        rounded="rounded-2xl"
        aria-labelledby={headingId}
        className={cn(
          "group p-5 transition-all duration-200",
          // Hermès-level hover lift: panel rises 1px, border tightens.
          "hover:-translate-y-[1px] hover:shadow-tp-hero",
          "hover:border-tp-amber/30",
          isDeparting && "pointer-events-none",
        )}
      >
        {/* ── Head row ──────────────────────────────────────────── */}
        <div className="flex flex-wrap items-start gap-x-3 gap-y-2">
          <div className="flex min-w-0 flex-1 flex-col gap-2">
            <div className="flex flex-wrap items-center gap-2">
              <KindBadge kind={proposal.kind} />
              <RiskBadge
                risk={proposal.risk}
                label={t(`evolution.tp.${risk.label}`)}
              />
              <span className="font-mono text-[10.5px] uppercase tracking-[0.08em] text-tp-ink-4">
                #{proposal.id}
              </span>
            </div>
            <h2
              id={headingId}
              className="font-mono text-[12.5px] leading-tight text-tp-ink"
            >
              <span className="text-tp-ink-3">
                {t("evolution.tp.cardTargetLabel")} ·{" "}
              </span>
              <span className="break-all">{proposal.target}</span>
            </h2>
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-tp-ink-3">
              <span>{ageLabel}</span>
              <span aria-hidden className="text-tp-ink-4">
                ·
              </span>
              <span>
                {t("evolution.tp.cardSignalsLabel", {
                  n: proposal.signal_ids.length,
                })}
              </span>
              {proposal.trace_ids.length > 0 ? (
                <>
                  <span aria-hidden className="text-tp-ink-4">
                    ·
                  </span>
                  <span className="font-mono text-tp-ink-4">
                    {t("evolution.tp.cardTraceLabel")} ·{" "}
                    {proposal.trace_ids.slice(0, 2).join(" ")}
                    {proposal.trace_ids.length > 2 ? " …" : null}
                  </span>
                </>
              ) : null}
            </div>
          </div>

          {/* ── Actions: collapse into inline-deny when denying ──── */}
          {denyOpen ? null : (
            <div className="flex shrink-0 items-center gap-2">
              <button
                type="button"
                onClick={handleApprove}
                disabled={disabled}
                aria-label={t("evolution.tp.approve")}
                className={cn(
                  "inline-flex items-center gap-1.5 rounded-full px-3.5 py-1 text-[12px] font-medium",
                  "bg-tp-amber text-[#1a120d] shadow-tp-primary",
                  "transition-transform duration-150 hover:-translate-y-[1px] active:translate-y-0",
                  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/55",
                  "disabled:pointer-events-none disabled:opacity-50",
                )}
              >
                <Check aria-hidden className="h-3.5 w-3.5" />
                {t("evolution.tp.approve")}
              </button>
              <button
                type="button"
                onClick={handleDenyClick}
                disabled={disabled}
                aria-label={t("evolution.tp.deny")}
                className={cn(
                  "inline-flex items-center gap-1.5 rounded-full border px-3.5 py-1 text-[12px] font-medium",
                  "border-tp-err/40 bg-transparent text-tp-err",
                  "transition-colors hover:bg-tp-err-soft",
                  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-err/50",
                  "disabled:pointer-events-none disabled:opacity-50",
                )}
              >
                <X aria-hidden className="h-3.5 w-3.5" />
                {t("evolution.tp.deny")}
              </button>
            </div>
          )}
        </div>

        {/* ── Inline deny input (replaces actions) ──────────────── */}
        {denyOpen ? (
          <div
            className={cn(
              "mt-4 flex flex-col gap-2 rounded-xl border p-3",
              "border-tp-err/30 bg-tp-err-soft/40",
            )}
          >
            <label
              htmlFor={`${headingId}-reason`}
              className="font-mono text-[10.5px] uppercase tracking-[0.08em] text-tp-err/85"
            >
              {t("evolution.tp.denyInlineLabel")}
            </label>
            <input
              id={`${headingId}-reason`}
              type="text"
              autoFocus
              value={denyReason}
              onChange={(e) => setDenyReason(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") handleDenyConfirm();
                if (e.key === "Escape") handleDenyCancel();
              }}
              placeholder={t("evolution.tp.denyInlinePlaceholder")}
              disabled={disabled}
              className={cn(
                "rounded-lg border px-3 py-2 text-[12.5px] text-tp-ink",
                "border-tp-err/35 bg-tp-glass-inner",
                "placeholder:text-tp-ink-4",
                "focus:outline-none focus:ring-2 focus:ring-tp-err/40",
              )}
            />
            <div className="flex items-center justify-end gap-2">
              <button
                type="button"
                onClick={handleDenyCancel}
                disabled={disabled}
                className={cn(
                  "rounded-full px-3 py-1 text-[11.5px] text-tp-ink-3",
                  "hover:bg-tp-glass-inner-hover hover:text-tp-ink-2",
                  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/40",
                )}
              >
                {t("evolution.tp.denyCancel")}
              </button>
              <button
                type="button"
                onClick={handleDenyConfirm}
                disabled={disabled}
                className={cn(
                  "inline-flex items-center gap-1.5 rounded-full px-3.5 py-1 text-[12px] font-medium",
                  "bg-tp-err text-white",
                  "transition-transform duration-150 hover:-translate-y-[1px]",
                  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-err/55",
                  "disabled:pointer-events-none disabled:opacity-50",
                )}
              >
                {t("evolution.tp.denyConfirm")}
              </button>
            </div>
          </div>
        ) : null}

        {/* ── Reasoning prose ─────────────────────────────────── */}
        <div className="mt-4 border-t border-tp-glass-edge pt-3">
          <div className="font-mono text-[10.5px] uppercase tracking-[0.08em] text-tp-ink-4">
            {t("evolution.tp.cardReasoningLabel")}
          </div>
          <p
            className={cn(
              "mt-1.5 text-[13px] leading-[1.7] text-tp-ink-2",
              !expanded && "line-clamp-2",
            )}
          >
            {proposal.reasoning}
          </p>
        </div>

        {/* ── Expand/collapse + diff ─────────────────────────── */}
        <div className="mt-3 flex items-center justify-between gap-2">
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            aria-expanded={expanded}
            aria-controls={`${headingId}-diff`}
            className={cn(
              "inline-flex items-center gap-1 rounded-full px-2 py-1",
              "font-mono text-[10.5px] uppercase tracking-[0.08em] text-tp-ink-3",
              "hover:bg-tp-glass-inner-hover hover:text-tp-ink-2",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/40",
            )}
          >
            {expanded ? (
              <ChevronUp aria-hidden className="h-3.5 w-3.5" />
            ) : (
              <ChevronDown aria-hidden className="h-3.5 w-3.5" />
            )}
            {expanded
              ? t("evolution.tp.cardCollapse")
              : t("evolution.tp.cardExpand")}
          </button>
        </div>

        {expanded ? (
          <div id={`${headingId}-diff`} className="mt-3">
            {proposal.diff.trim().length > 0 ? (
              <DiffBlock diff={proposal.diff} />
            ) : (
              <p className="text-[12px] italic text-tp-ink-4">
                {t("evolution.tp.cardDiffEmpty")}
              </p>
            )}
          </div>
        ) : null}
      </GlassPanel>
    </motion.div>
  );
}

// ─── Pieces ──────────────────────────────────────────────────────────────

function KindBadge({ kind }: { kind: string }) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full border px-2 py-[2px]",
        "border-tp-glass-edge bg-tp-glass-inner-strong",
        "font-mono text-[10.5px] tracking-wide text-tp-ink-2",
      )}
    >
      {kind}
    </span>
  );
}

function RiskBadge({
  risk,
  label,
}: {
  risk: EvolutionRisk;
  label: string;
}) {
  const tone = RISK_TONE[risk];
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full border px-2 py-[2px]",
        tone.border,
        tone.bg,
        tone.text,
        "font-mono text-[10.5px] uppercase tracking-[0.08em]",
      )}
    >
      <span
        aria-hidden
        className={cn(
          "h-[5px] w-[5px] rounded-full",
          risk === "low"
            ? "bg-tp-amber"
            : risk === "medium"
              ? "bg-tp-ember"
              : "bg-tp-err",
        )}
      />
      {label}
    </span>
  );
}

/** Render a unified diff with line-prefix coloring. */
function DiffBlock({ diff }: { diff: string }) {
  const lines = diff.split("\n");
  return (
    <pre
      className={cn(
        "rounded-lg border p-3 font-mono text-[11.5px] leading-[1.65]",
        "bg-tp-glass-inner border-tp-glass-edge text-tp-ink-2",
        "whitespace-pre overflow-x-auto",
      )}
    >
      {lines.map((line, i) => {
        const tone =
          line.startsWith("+++") || line.startsWith("---")
            ? "text-tp-ink-3"
            : line.startsWith("@@")
              ? "text-tp-amber"
              : line.startsWith("+")
                ? "text-tp-ok"
                : line.startsWith("-")
                  ? "text-tp-err"
                  : "text-tp-ink-2";
        return (
          <span key={i} className={cn("block", tone)}>
            {line || " "}
          </span>
        );
      })}
    </pre>
  );
}

function formatAgeLabel(
  ageMs: number,
  t: (key: string, opts?: Record<string, unknown>) => string,
): string {
  const sec = Math.floor(ageMs / 1000);
  if (sec < 60) {
    return t("evolution.tp.cardAgo", { s: Math.max(1, sec) });
  }
  const min = Math.floor(sec / 60);
  if (min < 60) {
    return t("evolution.tp.cardAgoMin", { m: min });
  }
  const hr = Math.floor(min / 60);
  return t("evolution.tp.cardAgoHr", { h: hr });
}

export default ProposalCard;
