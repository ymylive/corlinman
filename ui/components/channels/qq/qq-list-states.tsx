"use client";

import { useTranslation } from "react-i18next";
import { GlassPanel } from "@/components/ui/glass-panel";

/**
 * Offline / loading states for the QQ page. Mirrors the Plugins / Nodes
 * OfflineBlock pattern — soft glass panel with warm prose and a
 * single-line truncated diagnostic below.
 */

export function QqHeroSkeleton() {
  return (
    <GlassPanel
      variant="strong"
      aria-hidden
      className="h-[180px] animate-pulse"
    />
  );
}

export function QqOfflineBlock({ message }: { message?: string }) {
  const { t } = useTranslation();
  const firstLine = message
    ?.split(/\r?\n/)
    .find((ln) => ln.trim().length > 0)
    ?.trim();
  const isHtmlDump = firstLine?.startsWith("<");
  const short =
    !isHtmlDump && firstLine && firstLine.length > 180
      ? firstLine.slice(0, 180) + "…"
      : !isHtmlDump
        ? firstLine
        : undefined;
  return (
    <GlassPanel
      variant="soft"
      className="flex flex-col items-center gap-2 p-8 text-center"
      data-testid="qq-offline-block"
    >
      <div className="font-mono text-[11px] uppercase tracking-[0.12em] text-tp-err">
        {t("channels.qq.tp.offlineTitle")}
      </div>
      <p className="max-w-prose text-[13px] text-tp-ink-2">
        {t("channels.qq.tp.offlineHint")}
      </p>
      {short ? (
        <p
          className="max-w-full truncate font-mono text-[11px] text-tp-ink-4"
          title={message}
        >
          {short}
        </p>
      ) : null}
    </GlassPanel>
  );
}
