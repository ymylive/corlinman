"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import { Loader2, QrCode, RefreshCw } from "lucide-react";

import { cn } from "@/lib/utils";
import { GlassPanel } from "@/components/ui/glass-panel";
import { StreamPill } from "@/components/ui/stream-pill";
import type { QqConnection } from "./qq-util";
import { streamStateFor } from "./qq-util";

/**
 * `<QqHero>` — warm-glass hero strip for the QQ channel page.
 *
 * Lead pill + title + prose (NapCat endpoint, allowed-chat count, last
 * inbound age). CTA row: StreamPill (live/paused/throttled), reconnect
 * button, scan-login button. Mirrors the Scheduler/Plugins hero pattern.
 */

export interface QqHeroProps {
  connection: QqConnection;
  /** NapCat websocket URL (ws_url) or null. */
  wsUrl: string | null;
  /** Number of chats that have per-group overrides configured. */
  chatCount: number;
  /** Age string for the last recent message ("12s", "4m") or null. */
  lastInboundAgo: string | null;
  reconnecting: boolean;
  onReconnect: () => void;
  onScanLogin: () => void;
}

export function QqHero({
  connection,
  wsUrl,
  chatCount,
  lastInboundAgo,
  reconnecting,
  onReconnect,
  onScanLogin,
}: QqHeroProps) {
  const { t } = useTranslation();

  const streamState = streamStateFor(connection, reconnecting);
  const canReconnect = connection !== "offline";

  return (
    <GlassPanel variant="strong" as="section" className="relative overflow-hidden p-7">
      {/* Ambient warm-amber glow. */}
      <div
        aria-hidden
        className="pointer-events-none absolute bottom-[-90px] right-[-40px] h-[240px] w-[360px] rounded-full opacity-60 blur-3xl"
        style={{
          background:
            "radial-gradient(closest-side, var(--tp-amber-glow), transparent 70%)",
        }}
      />
      <div
        aria-hidden
        className="pointer-events-none absolute top-[-60px] left-[-40px] h-[180px] w-[260px] rounded-full opacity-40 blur-[50px]"
        style={{
          background:
            "radial-gradient(closest-side, color-mix(in oklch, var(--tp-ember) 35%, transparent), transparent 70%)",
        }}
      />

      <div className="relative flex min-w-0 flex-col gap-4">
        {/* Lead pill. */}
        <div className="inline-flex w-fit items-center gap-2.5 rounded-full border border-tp-glass-edge bg-tp-glass-inner-strong py-1 pl-2 pr-3 font-mono text-[11px] text-tp-ink-2">
          <span
            className={cn(
              "h-1.5 w-1.5 rounded-full",
              connection === "connected"
                ? "bg-tp-ok tp-breathe"
                : connection === "disconnected"
                  ? "bg-tp-err"
                  : "bg-tp-ink-4",
            )}
          />
          {t("channels.qq.tp.leadPill", {
            state: t(`channels.qq.tp.state.${connection}`),
            chats: chatCount,
          })}
        </div>

        <h1 className="text-balance font-sans text-[28px] font-semibold leading-[1.15] tracking-[-0.025em] text-tp-ink sm:text-[32px]">
          {t("channels.qq.tp.title")}
        </h1>

        <p className="max-w-[72ch] text-[14.5px] leading-[1.6] text-tp-ink-2">
          {connection === "connected" ? (
            <>
              {t("channels.qq.tp.proseConnected", {
                endpoint: wsUrl ?? t("channels.qq.tp.proseUnknownEndpoint"),
              })}
              {" "}
              {t("channels.qq.tp.proseChats", { n: chatCount })}
              {lastInboundAgo
                ? ` ${t("channels.qq.tp.proseLastInbound", { age: lastInboundAgo })}`
                : ` ${t("channels.qq.tp.proseNoRecent")}`}
            </>
          ) : connection === "disconnected" ? (
            <>{t("channels.qq.tp.proseDisconnected")}</>
          ) : connection === "disabled" ? (
            <>{t("channels.qq.tp.proseDisabled")}</>
          ) : connection === "offline" ? (
            <>{t("channels.qq.tp.proseOffline")}</>
          ) : (
            <>{t("channels.qq.tp.proseUnknown")}</>
          )}
        </p>

        <div className="mt-1 flex flex-wrap items-center gap-2.5">
          <StreamPill state={streamState} rate={t(`channels.qq.tp.pillRate.${streamState}`)} />

          <button
            type="button"
            onClick={onReconnect}
            disabled={!canReconnect || reconnecting}
            data-testid="qq-reconnect-btn"
            aria-label={t("channels.reconnect")}
            className={cn(
              "inline-flex items-center gap-2 rounded-lg border px-3 py-2 text-[13px] font-medium",
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
            {reconnecting
              ? t("channels.reconnecting")
              : t("channels.reconnect")}
          </button>

          <button
            type="button"
            onClick={onScanLogin}
            data-testid="qq-scan-login-btn"
            className="inline-flex items-center gap-2 rounded-lg border border-tp-glass-edge bg-tp-glass-inner px-3 py-2 text-[13px] font-medium text-tp-ink-2 transition-colors hover:bg-tp-glass-inner-hover hover:text-tp-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/40"
          >
            <QrCode className="h-3.5 w-3.5" aria-hidden />
            {t("channels.qq.scanLogin.openButton")}
          </button>
        </div>
      </div>
    </GlassPanel>
  );
}

export default QqHero;
