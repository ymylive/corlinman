"use client";

import * as React from "react";
import { useMutation } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { ChevronRight, Loader2 } from "lucide-react";

import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import {
  RERUN_NOT_IMPLEMENTED,
  replaySession,
  type ReplayMode,
  type ReplayResponse,
  type ReplayResult,
  type SessionSummary,
} from "@/lib/api/sessions";

import { TranscriptView } from "./transcript-view";

/**
 * Replay dialog — opens from a session-row Replay button. Defaults to
 * `mode = "transcript"` (the read-only deterministic dump).
 *
 * The `rerun` mode is rendered as a disabled radio with a `title` tooltip
 * "coming in Wave 2.5". When the Rust backend ships its full diff renderer
 * (Wave 2.5) the disabled flag flips off and the response's `summary.rerun_diff`
 * sentinel goes from `"not_implemented_yet"` to a real diff payload — at
 * that point this component renders the diff view via a follow-up component;
 * v1 just shows the placeholder explainer block when it sees the sentinel.
 *
 * The dialog also surfaces breadcrumbs as `Admin / Sessions / <key>` so the
 * navigation context matches the rest of the admin shell. Breadcrumbs proper
 * live in `<Breadcrumbs>` (top of the admin shell) — here we keep the same
 * three-segment header inline so the dialog is self-contained.
 */

interface ReplayDialogProps {
  /** Source session row. `null` closes the dialog. */
  session: SessionSummary | null;
  /** Called when the dialog wants to close (Esc, click-outside, x-button). */
  onClose: () => void;
}

export function ReplayDialog({ session, onClose }: ReplayDialogProps) {
  const { t } = useTranslation();
  const [mode] = React.useState<ReplayMode>("transcript");
  const sessionKey = session?.session_key ?? null;

  const mutation = useMutation<ReplayResult, Error, { mode: ReplayMode }>({
    mutationFn: async ({ mode }) => {
      if (!sessionKey) throw new Error("no_session");
      return replaySession(sessionKey, { mode });
    },
  });

  // Auto-fire transcript replay when the dialog opens for a new session.
  // Reset is also explicit — if the dialog closes mid-fetch, throw away the
  // stale result so the next open doesn't flash old data.
  React.useEffect(() => {
    if (!sessionKey) {
      mutation.reset();
      return;
    }
    mutation.mutate({ mode: "transcript" });
    // We intentionally exclude `mutation` from deps — adding it triggers a
    // re-fetch loop because react-query rebuilds the closure on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionKey]);

  const open = sessionKey !== null;
  const onOpenChange = (next: boolean): void => {
    if (!next) onClose();
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className={cn(
          "max-w-3xl gap-0 rounded-2xl border-tp-glass-edge bg-tp-glass-2 p-0",
          "backdrop-blur-glass-strong backdrop-saturate-glass-strong",
          "shadow-tp-hero",
        )}
        data-testid="replay-dialog"
      >
        <DialogHeader className="space-y-2 border-b border-tp-glass-edge px-6 py-4 text-left">
          <ReplayBreadcrumbs sessionKey={sessionKey ?? ""} />
          <DialogTitle className="font-mono text-[15px] font-medium text-tp-ink">
            {t("sessions.dialogTitle", { key: sessionKey ?? "" })}
          </DialogTitle>
          <DialogDescription className="text-xs text-tp-ink-3">
            {t("sessions.dialogDescription")}
          </DialogDescription>
        </DialogHeader>

        <div className="max-h-[70vh] overflow-y-auto px-6 py-4">
          <ModeSelector mode={mode} />
          <div className="mt-4">
            <ReplayBody result={mutation.data} mutation={mutation} />
          </div>
        </div>

        <div className="flex items-center justify-end gap-2 border-t border-tp-glass-edge px-6 py-3">
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={onClose}
            data-testid="replay-dialog-close"
          >
            {t("common.close")}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

/** Three-segment crumb echoing the admin shell breadcrumbs. */
function ReplayBreadcrumbs({ sessionKey }: { sessionKey: string }) {
  const { t } = useTranslation();
  return (
    <nav
      aria-label="replay breadcrumb"
      className="flex items-center gap-1 text-[11px] text-tp-ink-3"
      data-testid="replay-breadcrumbs"
    >
      <span>{t("breadcrumbs.dashboard")}</span>
      <ChevronRight aria-hidden="true" className="h-3 w-3" />
      <span>{t("breadcrumbs.sessions")}</span>
      <ChevronRight aria-hidden="true" className="h-3 w-3" />
      <span className="font-mono text-tp-ink-2" data-testid="replay-breadcrumb-key">
        {sessionKey}
      </span>
    </nav>
  );
}

/**
 * Mode picker. Native radios — matches the W1 tenant-switcher discipline of
 * "no extra primitives unless we need them". `rerun` is disabled in v1 with
 * a `title` tooltip; once Wave 2.5 ships the diff renderer, the `disabled`
 * flag flips off.
 */
function ModeSelector({ mode }: { mode: ReplayMode }) {
  const { t } = useTranslation();
  return (
    <fieldset
      className="flex flex-col gap-2 rounded-md border border-tp-glass-edge bg-tp-glass-inner px-3 py-2"
      data-testid="replay-mode-selector"
    >
      <legend className="px-1 text-[11px] uppercase tracking-wide text-tp-ink-3">
        {t("sessions.modeLabel")}
      </legend>
      <label className="flex items-center gap-2 text-xs text-tp-ink-2">
        <input
          type="radio"
          name="replay-mode"
          value="transcript"
          checked={mode === "transcript"}
          readOnly
          data-testid="replay-mode-transcript"
          className="accent-tp-amber"
        />
        <span>{t("sessions.modeTranscript")}</span>
      </label>
      <label
        className="flex cursor-not-allowed items-center gap-2 text-xs text-tp-ink-3 opacity-60"
        title={t("sessions.modeRerunComingSoon")}
        data-testid="replay-mode-rerun-label"
      >
        <input
          type="radio"
          name="replay-mode"
          value="rerun"
          checked={false}
          disabled
          aria-disabled="true"
          readOnly
          data-testid="replay-mode-rerun"
          className="accent-tp-amber"
        />
        <span>{t("sessions.modeRerun")}</span>
        <span className="ml-auto rounded-full border border-tp-glass-edge bg-tp-glass-inner px-1.5 py-0.5 font-mono text-[10px] text-tp-ink-3">
          {t("sessions.modeRerunComingSoon")}
        </span>
      </label>
    </fieldset>
  );
}

interface ReplayBodyProps {
  result: ReplayResult | undefined;
  mutation: {
    isPending: boolean;
    isError: boolean;
    error: Error | null;
  };
}

function ReplayBody({ result, mutation }: ReplayBodyProps) {
  const { t } = useTranslation();

  if (mutation.isPending || result === undefined) {
    return <ReplaySkeleton />;
  }

  if (mutation.isError) {
    const msg = mutation.error?.message ?? t("common.error");
    return (
      <div
        role="alert"
        className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive"
        data-testid="replay-error"
      >
        {t("sessions.replayFailed", { msg })}
      </div>
    );
  }

  if (result.kind === "not_found") {
    return (
      <div
        role="alert"
        className="rounded-md border border-tp-glass-edge bg-tp-glass-inner px-4 py-3 text-xs text-tp-ink-2"
        data-testid="replay-not-found"
      >
        <div className="font-medium text-tp-ink">
          {t("sessions.notFoundTitle")}
        </div>
        <div className="mt-1 text-tp-ink-3">
          {t("sessions.notFoundHint", { key: result.session_key })}
        </div>
      </div>
    );
  }

  if (result.kind === "disabled") {
    return (
      <div
        role="alert"
        className="rounded-md border border-tp-glass-edge bg-tp-glass-inner px-4 py-3 text-xs text-tp-ink-2"
        data-testid="replay-disabled"
      >
        <div className="font-medium text-tp-ink">
          {t("sessions.sessionsDisabledTitle")}
        </div>
        <div className="mt-1 text-tp-ink-3">
          {t("sessions.sessionsDisabledHint")}
        </div>
      </div>
    );
  }

  return <ReplayPayload replay={result.replay} />;
}

function ReplayPayload({ replay }: { replay: ReplayResponse }) {
  const { t } = useTranslation();
  const isRerunStub =
    replay.mode === "rerun" &&
    replay.summary.rerun_diff === RERUN_NOT_IMPLEMENTED;

  return (
    <div className="space-y-3">
      {isRerunStub ? (
        <div
          role="note"
          className="rounded-md border border-dashed border-tp-glass-edge bg-tp-glass-inner px-4 py-3 text-xs text-tp-ink-2"
          data-testid="replay-rerun-stub"
        >
          <div className="font-medium text-tp-ink">
            {t("sessions.rerunNotImplementedTitle")}
          </div>
          <div className="mt-1 text-tp-ink-3">
            {t("sessions.rerunNotImplementedHint")}
          </div>
        </div>
      ) : null}

      <ReplaySummaryRow
        messageCount={replay.summary.message_count}
        tenantId={replay.summary.tenant_id}
      />
      <TranscriptView transcript={replay.transcript} />
    </div>
  );
}

function ReplaySummaryRow({
  messageCount,
  tenantId,
}: {
  messageCount: number;
  tenantId: string;
}) {
  const { t } = useTranslation();
  return (
    <div
      className="flex flex-wrap items-center gap-3 rounded-md border border-tp-glass-edge bg-tp-glass-inner px-3 py-2 text-[11px] text-tp-ink-3"
      data-testid="replay-summary"
    >
      <span>
        <span className="font-medium text-tp-ink-2">
          {t("sessions.summaryMessageCount")}:
        </span>{" "}
        <span className="font-mono text-tp-ink">{messageCount}</span>
      </span>
      <span aria-hidden="true">·</span>
      <span>
        <span className="font-medium text-tp-ink-2">
          {t("sessions.summaryTenantId")}:
        </span>{" "}
        <span className="font-mono text-tp-ink">{tenantId}</span>
      </span>
    </div>
  );
}

function ReplaySkeleton() {
  const { t } = useTranslation();
  return (
    <div
      className="flex flex-col gap-3"
      role="status"
      aria-live="polite"
      aria-label={t("sessions.replayLoading")}
      data-testid="replay-loading"
    >
      <div className="flex items-center gap-2 text-xs text-tp-ink-3">
        <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
        <span>{t("sessions.replayLoading")}</span>
      </div>
      <Skeleton className="h-12 w-3/5" />
      <Skeleton className="h-16 w-4/5 self-end" />
      <Skeleton className="h-10 w-2/5" />
    </div>
  );
}
