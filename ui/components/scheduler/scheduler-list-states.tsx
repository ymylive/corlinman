"use client";

import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";
import { GlassPanel } from "@/components/ui/glass-panel";

/**
 * Non-list states for the Scheduler page — loading skeleton, gateway
 * offline, and empty/filter-empty. Kept out of `page.tsx` so the page
 * stays small and the visual states are easy to eyeball side-by-side.
 *
 * Copy mirrors the Plugins page patterns (offline truncation, filter-aware
 * empty messaging). See `ui/app/(admin)/plugins/page.tsx` for the parent
 * of the pattern.
 */

export function SchedulerListSkeleton() {
  return (
    <div aria-hidden className="flex flex-col gap-2">
      {Array.from({ length: 4 }).map((_, i) => (
        <div
          key={i}
          className={cn(
            "h-[72px] animate-pulse rounded-2xl border border-tp-glass-edge",
            "bg-tp-glass-inner/70",
          )}
        />
      ))}
    </div>
  );
}

export function SchedulerOfflineBlock({ message }: { message?: string }) {
  const { t } = useTranslation();
  // A raw fetch error can be the gateway's full 404 HTML body — cap to a
  // single line so the panel doesn't blow up the layout.
  const firstLine = message
    ?.split(/\r?\n/)
    .find((ln) => ln.trim().length > 0)
    ?.trim();
  // Raw HTML dumps (the gateway returning a 404 page) add no signal and
  // look broken. Suppress them outright; keep plain-text diagnostics.
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
    >
      <div className="font-mono text-[11px] uppercase tracking-[0.12em] text-tp-err">
        {t("scheduler.tp.offlineTitle")}
      </div>
      <p className="max-w-prose text-[13px] text-tp-ink-2">
        {t("scheduler.tp.offlineHint")}
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

export function SchedulerEmptyBlock({ hasAnyJobs }: { hasAnyJobs: boolean }) {
  const { t } = useTranslation();
  return (
    <GlassPanel
      variant="soft"
      className="flex flex-col items-center gap-2 p-8 text-center"
    >
      <div className="text-[14px] font-medium text-tp-ink">
        {hasAnyJobs
          ? t("scheduler.tp.filterEmptyTitle")
          : t("scheduler.tp.emptyTitle")}
      </div>
      <p className="max-w-prose text-[13px] text-tp-ink-3">
        {hasAnyJobs
          ? t("scheduler.tp.filterEmptyHint")
          : t("scheduler.tp.emptyHint")}
      </p>
    </GlassPanel>
  );
}
