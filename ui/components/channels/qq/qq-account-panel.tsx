"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import { Loader2, RefreshCw } from "lucide-react";

import { cn } from "@/lib/utils";
import { GlassPanel } from "@/components/ui/glass-panel";
import type { QqStatus } from "@/lib/api";
import type { QqConnection } from "./qq-util";

/**
 * Left column of the QQ config grid — NapCat endpoint, configured self_ids,
 * and a reconnect CTA mirroring the hero. All fields read-only; writes are
 * done via the admin config UI (config page) rather than here.
 */

export interface QqAccountPanelProps {
  status: QqStatus | undefined;
  connection: QqConnection;
  reconnecting: boolean;
  onReconnect: () => void;
}

export function QqAccountPanel({
  status,
  connection,
  reconnecting,
  onReconnect,
}: QqAccountPanelProps) {
  const { t } = useTranslation();
  const canReconnect = connection !== "offline";

  return (
    <GlassPanel
      variant="soft"
      as="section"
      className="flex flex-col gap-4 p-5"
      data-testid="qq-account-panel"
    >
      <div className="flex items-center justify-between gap-2">
        <h2 className="text-[14px] font-medium text-tp-ink">
          {t("channels.qq.tp.accountTitle")}
        </h2>
        <span
          className={cn(
            "inline-flex items-center gap-1.5 rounded-full border px-2 py-[2px] font-mono text-[10px] uppercase tracking-[0.08em]",
            connection === "connected"
              ? "border-tp-ok/25 bg-tp-ok-soft text-tp-ok"
              : connection === "disconnected"
                ? "border-tp-err/25 bg-tp-err-soft text-tp-err"
                : "border-tp-glass-edge bg-tp-glass-inner text-tp-ink-3",
          )}
        >
          <span
            aria-hidden
            className={cn(
              "h-1.5 w-1.5 rounded-full",
              connection === "connected"
                ? "bg-tp-ok tp-breathe"
                : connection === "disconnected"
                  ? "bg-tp-err"
                  : "bg-tp-ink-4",
            )}
          />
          {t(`channels.qq.tp.state.${connection}`)}
        </span>
      </div>

      <dl className="flex flex-col gap-3 text-[12.5px]">
        <Row label="ws_url" value={status?.ws_url ?? "(none)"} mono />
        <Row
          label="self_ids"
          value={
            status?.self_ids && status.self_ids.length > 0
              ? `[${status.self_ids.join(", ")}]`
              : "[]"
          }
          mono
        />
        <Row
          label={t("channels.qq.tp.runtimeLabel")}
          value={t(`channels.qq.tp.state.${connection}`)}
        />
      </dl>

      <button
        type="button"
        onClick={onReconnect}
        disabled={!canReconnect || reconnecting}
        aria-label={t("channels.reconnect")}
        className={cn(
          "inline-flex w-fit items-center gap-2 rounded-lg border px-3 py-2 text-[13px] font-medium",
          "border-tp-amber/35 bg-tp-amber-soft text-tp-amber",
          "transition-colors hover:bg-[color-mix(in_oklch,var(--tp-amber)_22%,transparent)]",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/50",
          "disabled:cursor-not-allowed disabled:opacity-60",
        )}
      >
        {reconnecting ? (
          <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
        ) : (
          <RefreshCw className="h-3.5 w-3.5" aria-hidden />
        )}
        {reconnecting ? t("channels.reconnecting") : t("channels.reconnect")}
      </button>
    </GlassPanel>
  );
}

function Row({
  label,
  value,
  mono,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="flex min-w-0 items-center gap-3">
      <dt className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-tp-ink-4">
        {label}
      </dt>
      <dd
        className={cn(
          "min-w-0 flex-1 truncate rounded-md border border-tp-glass-edge bg-tp-glass-inner px-2 py-1 text-tp-ink-2",
          mono ? "font-mono text-[11.5px]" : "text-[12.5px]",
        )}
        title={value}
      >
        {value}
      </dd>
    </div>
  );
}

export default QqAccountPanel;
