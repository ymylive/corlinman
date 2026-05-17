"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import { Pause, Play, Settings2, Eye, PlayCircle } from "lucide-react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { ProfileCuratorState } from "@/lib/api";

/**
 * One profile card row on the curator surface. Renders:
 *
 *   - slug + display state (▶ running / ⏸ paused)
 *   - the three threshold pills + an "Edit thresholds" affordance
 *   - last-run summary line (or "never ran" fallback)
 *   - four buttons: Preview · Run now · Pause/Resume · Edit thresholds
 *
 * State mutation is the parent page's responsibility — this component
 * only emits callbacks. That keeps the page-level cache invalidation
 * logic in one place (the page) and lets us test the card with simple
 * `vi.fn()` spies.
 */
export interface ProfileCuratorCardProps {
  profile: ProfileCuratorState;
  onPreview: () => void;
  onRunNow: () => void;
  onTogglePause: () => void;
  onEditThresholds: () => void;
  /** When `true`, disable every action button (a sibling mutation is in
   * flight). The card itself stays visible — only the affordances dim. */
  busy?: boolean;
}

export function ProfileCuratorCard({
  profile,
  onPreview,
  onRunNow,
  onTogglePause,
  onEditThresholds,
  busy = false,
}: ProfileCuratorCardProps) {
  const { t } = useTranslation();

  const statusLabel = profile.paused
    ? t("evolution.curator.paused")
    : t("evolution.curator.running");

  const lastRunLabel = profile.last_review_at
    ? t("evolution.curator.lastRun", {
        when: formatRelative(profile.last_review_at),
      })
    : t("evolution.curator.neverRan");

  return (
    <div
      data-testid={`profile-card-${profile.slug}`}
      className={cn(
        "rounded-2xl border border-tp-glass-edge bg-tp-glass-inner/70 p-4",
        "flex flex-col gap-3",
      )}
    >
      {/* Title row — slug + status */}
      <div className="flex items-start justify-between gap-3">
        <div className="flex flex-col gap-0.5">
          <div className="flex items-center gap-2">
            <span className="font-mono text-sm font-semibold text-tp-ink-1">
              {profile.slug}
            </span>
            <StatusPill paused={profile.paused} label={statusLabel} />
          </div>
          <div className="text-[11.5px] text-tp-ink-3">{lastRunLabel}</div>
          {profile.last_review_summary ? (
            <div className="font-mono text-[10.5px] text-tp-ink-3">
              {profile.last_review_summary}
            </div>
          ) : null}
        </div>
        <div className="flex flex-col items-end gap-1 text-[10.5px] text-tp-ink-3">
          <span>
            {t("evolution.curator.runCount", { n: profile.run_count })}
          </span>
          <CountPills profile={profile} />
        </div>
      </div>

      {/* Threshold pills */}
      <div className="flex flex-wrap items-center gap-2 text-[11.5px]">
        <ThresholdPill
          label={t("evolution.thresholds.interval")}
          value={`${profile.interval_hours}h`}
        />
        <ThresholdPill
          label={t("evolution.thresholds.stale")}
          value={`${profile.stale_after_days}d`}
        />
        <ThresholdPill
          label={t("evolution.thresholds.archive")}
          value={`${profile.archive_after_days}d`}
        />
      </div>

      {/* Action row */}
      <div className="flex flex-wrap items-center gap-2 pt-1">
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={onPreview}
          disabled={busy}
          aria-label={t("evolution.curator.preview")}
        >
          <Eye className="mr-1.5 h-3.5 w-3.5" />
          {t("evolution.curator.preview")}
        </Button>
        <Button
          type="button"
          size="sm"
          onClick={onRunNow}
          disabled={busy || profile.paused}
          aria-label={t("evolution.curator.runNow")}
        >
          <PlayCircle className="mr-1.5 h-3.5 w-3.5" />
          {t("evolution.curator.runNow")}
        </Button>
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={onTogglePause}
          disabled={busy}
          aria-label={
            profile.paused
              ? t("evolution.curator.resume")
              : t("evolution.curator.pause")
          }
        >
          {profile.paused ? (
            <Play className="mr-1.5 h-3.5 w-3.5" />
          ) : (
            <Pause className="mr-1.5 h-3.5 w-3.5" />
          )}
          {profile.paused
            ? t("evolution.curator.resume")
            : t("evolution.curator.pause")}
        </Button>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          onClick={onEditThresholds}
          disabled={busy}
          aria-label={t("evolution.curator.editThresholds")}
        >
          <Settings2 className="mr-1.5 h-3.5 w-3.5" />
          {t("evolution.curator.editThresholds")}
        </Button>
      </div>
    </div>
  );
}

function StatusPill({ paused, label }: { paused: boolean; label: string }) {
  return (
    <span
      data-testid={paused ? "status-paused" : "status-running"}
      className={cn(
        "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10.5px] font-medium",
        paused
          ? "border-zinc-500/30 bg-zinc-500/10 text-zinc-700 dark:text-zinc-300"
          : "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
      )}
    >
      <span
        aria-hidden
        className={cn(
          "h-1.5 w-1.5 rounded-full",
          paused ? "bg-zinc-500" : "bg-emerald-500",
        )}
      />
      {label}
    </span>
  );
}

function ThresholdPill({ label, value }: { label: string; value: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded-md border border-tp-glass-edge px-2 py-1">
      <span className="text-tp-ink-3">{label}</span>
      <span className="font-mono font-semibold text-tp-ink-1">{value}</span>
    </span>
  );
}

function CountPills({ profile }: { profile: ProfileCuratorState }) {
  const { t } = useTranslation();
  const counts = profile.skill_counts;
  if (counts.total === 0) return null;
  return (
    <div className="flex items-center gap-1.5">
      {counts.active > 0 ? (
        <Count
          label={t("evolution.skill.state.active")}
          value={counts.active}
        />
      ) : null}
      {counts.stale > 0 ? (
        <Count
          label={t("evolution.skill.state.stale")}
          value={counts.stale}
        />
      ) : null}
      {counts.archived > 0 ? (
        <Count
          label={t("evolution.skill.state.archived")}
          value={counts.archived}
        />
      ) : null}
    </div>
  );
}

function Count({ label, value }: { label: string; value: number }) {
  return (
    <span className="inline-flex items-center gap-1 font-mono">
      <span>{label}</span>
      <span className="font-semibold text-tp-ink-1">{value}</span>
    </span>
  );
}

/**
 * Render an ISO-8601 timestamp as a short relative label. Defensive
 * against malformed strings → returns "?". Mirrors the prose helpers in
 * the existing evolution page so the card's last-run line matches the
 * other panels visually.
 */
export function formatRelative(iso: string): string {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "?";
  const diffMs = Date.now() - t;
  if (diffMs < 0) return new Date(t).toLocaleString();
  const sec = Math.floor(diffMs / 1000);
  if (sec < 60) return `${sec}s ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.floor(hr / 24);
  return `${day}d ago`;
}
