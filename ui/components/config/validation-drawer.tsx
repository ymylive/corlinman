"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import { AlertTriangle, Check } from "lucide-react";

import { cn } from "@/lib/utils";
import { GlassPanel } from "@/components/ui/glass-panel";
import { DetailDrawer } from "@/components/ui/detail-drawer";
import { JsonView } from "@/components/ui/json-view";
import type { ConfigPostResponse } from "@/lib/api";

/**
 * Right-rail drawer that surfaces the most recent validate / save response.
 * Renders as a `<DetailDrawer>` (inline, not modal) — matches the
 * Logs/Approvals pattern. Body breaks into three sections:
 *   - `restart` — warn pill when the backend flagged restart-required.
 *   - `issues`  — one card per issue, amber for warn / red for error.
 *   - `raw`     — `<JsonView>` of the complete response for copy/debug.
 *
 * When `clean` (ok + zero issues), the issues section is replaced with a
 * short "all clear" message.
 */
export function ValidationDrawer({
  result,
  onClose,
}: {
  result: ConfigPostResponse;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const clean = result.status === "ok" && result.issues.length === 0;
  const title = clean
    ? t("config.tp.validationOkTitle")
    : result.issues.length === 1
      ? t("config.issueTitleSingular")
      : t("config.issueTitle", { n: result.issues.length });
  const meta = (
    <>
      <span
        className={cn(
          "inline-flex items-center gap-1 rounded-full border px-2 py-[1px] font-mono text-[10px] font-medium uppercase tracking-wide",
          clean
            ? "border-tp-ok/30 bg-tp-ok-soft text-tp-ok"
            : "border-tp-err/30 bg-tp-err-soft text-tp-err",
        )}
      >
        {clean ? <Check className="h-3 w-3" /> : <AlertTriangle className="h-3 w-3" />}
        {clean ? t("config.statusOk") : t("config.statusInvalid")}
      </span>
      {result.version ? (
        <span className="rounded-md border border-tp-glass-edge bg-tp-glass-inner px-1.5 py-[1px] font-mono text-[10.5px] text-tp-ink-3">
          v{result.version}
        </span>
      ) : null}
      <button
        type="button"
        onClick={onClose}
        aria-label={t("config.tp.validationCloseAria")}
        className="ml-auto inline-flex h-6 items-center justify-center rounded-md border border-tp-glass-edge bg-tp-glass-inner px-2 text-[11px] text-tp-ink-3 transition-colors hover:bg-tp-glass-inner-hover hover:text-tp-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/40"
      >
        {t("common.close")}
      </button>
    </>
  );

  return (
    <DetailDrawer
      title={title}
      subsystem={t("config.tp.validationDrawerSubsystem")}
      meta={meta}
      className="max-h-[calc(100vh-32px)]"
    >
      {result.requires_restart.length > 0 ? (
        <DetailDrawer.Section label="restart">
          <span className="inline-flex items-center gap-1.5 rounded-full border border-tp-warn/30 bg-tp-warn-soft px-2 py-[2px] font-mono text-[10.5px] font-medium text-tp-warn">
            {t("config.tp.validationRestartTag", {
              list: result.requires_restart.join(", "),
            })}
          </span>
        </DetailDrawer.Section>
      ) : null}

      {clean ? (
        <DetailDrawer.Section label={t("config.tp.validationDrawerTitle")}>
          <p className="text-[13px] text-tp-ink-2">{t("config.tp.validationOkHint")}</p>
        </DetailDrawer.Section>
      ) : (
        <DetailDrawer.Section label={t("config.tp.validationIssuesSection")}>
          <ul className="flex flex-col gap-2">
            {result.issues.map((iss, i) => (
              <li
                key={`${iss.path}-${i}`}
                className={cn(
                  "flex items-start gap-2 rounded-md border px-2 py-1.5 text-[12px]",
                  iss.level === "error"
                    ? "border-tp-err/25 bg-tp-err-soft"
                    : "border-tp-warn/25 bg-tp-warn-soft",
                )}
              >
                <span
                  className={cn(
                    "shrink-0 rounded-full border px-1.5 py-[1px] font-mono text-[9.5px] font-medium uppercase tracking-wide",
                    iss.level === "error"
                      ? "border-tp-err/30 text-tp-err"
                      : "border-tp-warn/30 text-tp-warn",
                  )}
                >
                  {iss.level}
                </span>
                <code className="shrink-0 font-mono text-[11.5px] text-tp-ink-3">
                  {iss.path}
                </code>
                <span className="flex-1 text-tp-ink-2">{iss.message}</span>
              </li>
            ))}
          </ul>
        </DetailDrawer.Section>
      )}

      <DetailDrawer.Section label={t("config.tp.validationRawSection")}>
        <JsonView value={result} />
      </DetailDrawer.Section>
    </DetailDrawer>
  );
}

/** Placeholder when no result exists (or the drawer is collapsed). */
export function IdleDrawer({
  hasResult,
  onOpen,
}: {
  hasResult: boolean;
  onOpen: () => void;
}) {
  const { t } = useTranslation();
  return (
    <GlassPanel
      variant="subtle"
      className="flex min-h-[200px] flex-col items-center justify-center gap-2 p-6 text-center"
    >
      <div className="font-mono text-[10.5px] uppercase tracking-[0.12em] text-tp-ink-4">
        {t("config.tp.validationDrawerTitle")}
      </div>
      <p className="max-w-[34ch] text-[12.5px] text-tp-ink-3">
        {t("config.tp.statValidatorsFoot")}
      </p>
      {hasResult ? (
        <button
          type="button"
          onClick={onOpen}
          className="mt-2 inline-flex items-center gap-1 rounded-md border border-tp-glass-edge bg-tp-glass-inner px-2.5 py-1 text-[11.5px] font-medium text-tp-ink-2 transition-colors hover:bg-tp-glass-inner-hover hover:text-tp-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/40"
        >
          {t("config.tp.validationDrawerTitle")}
        </button>
      ) : null}
    </GlassPanel>
  );
}
