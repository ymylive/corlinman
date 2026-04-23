"use client";

import { useTranslation } from "react-i18next";
import { GlassPanel } from "@/components/ui/glass-panel";

/**
 * Offline / empty panels for the Nodes page. Mirrors the pattern used on
 * Plugins/Skills/Characters: a soft glass panel with warm prose and a
 * single-line truncated diagnostic below (for the offline case).
 */

export function OfflineBlock({ message }: { message?: string }) {
  const { t } = useTranslation();
  // Truncate diagnostic messages — a raw fetch error can be the gateway's
  // full 404 HTML body, which blows up the layout. Cap to a single line.
  const firstLine = message
    ?.split(/\r?\n/)
    .find((ln) => ln.trim().length > 0)
    ?.trim();
  const short =
    firstLine && firstLine.length > 180
      ? firstLine.slice(0, 180) + "…"
      : firstLine;
  return (
    <GlassPanel
      variant="soft"
      className="flex flex-col items-center gap-2 p-8 text-center"
      data-testid="nodes-offline-block"
    >
      <div className="font-mono text-[11px] uppercase tracking-[0.12em] text-tp-err">
        {t("nodes.tp.offlineTitle")}
      </div>
      <p className="max-w-prose text-[13px] text-tp-ink-2">
        {t("nodes.tp.offlineHint")}
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

export function EmptyBlock() {
  const { t } = useTranslation();
  return (
    <GlassPanel
      variant="soft"
      className="flex flex-col items-center gap-2 p-8 text-center"
      data-testid="nodes-empty-block"
    >
      <div className="text-[14px] font-medium text-tp-ink">
        {t("nodes.tp.emptyTitle")}
      </div>
      <p className="max-w-prose text-[13px] text-tp-ink-3">
        {t("nodes.tp.emptyHint")}
      </p>
    </GlassPanel>
  );
}

export default OfflineBlock;
