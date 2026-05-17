"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import { Pin, PinOff, Search } from "lucide-react";

import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type {
  CuratorSkillOrigin,
  CuratorSkillState,
  SkillSummary,
} from "@/lib/api";

import { SkillBadge } from "./skill-badge";

/**
 * Filterable skill table for one profile.
 *
 * Three filters compose:
 *   - State (active / stale / archived / all)
 *   - Origin (bundled / user-requested / agent-created / all)
 *   - Search (substring on name OR description, case-insensitive)
 *
 * The filter state lives inside this component so the parent page
 * doesn't have to thread three more controlled props through. Filters
 * are surfaced via the `onFiltersChange` callback so the parent CAN
 * push them up if it wants server-side filtering — by default the
 * client filter mode is enough for the tens-of-skills scale the
 * curator targets.
 */
export type SkillStateFilter = CuratorSkillState | "all";
export type SkillOriginFilter = CuratorSkillOrigin | "all";

export interface SkillListProps {
  skills: SkillSummary[];
  loading?: boolean;
  /** Fires with the post-toggle pin value (the row already shows the
   * skill's CURRENT pin state; the parent flips it via the API and
   * refetches). */
  onTogglePin: (name: string, nextPinned: boolean) => void;
  /** Optional escape hatch — the parent can read filter state to drive
   * a server-side fetch instead of relying on the in-component filter
   * pass. We still do the client-side filter so the loading flash
   * doesn't show stale rows. */
  onFiltersChange?: (filters: {
    state: SkillStateFilter;
    origin: SkillOriginFilter;
    search: string;
  }) => void;
}

const STATE_OPTIONS: { value: SkillStateFilter; labelKey: string }[] = [
  { value: "all", labelKey: "evolution.skill.filterStateAll" },
  { value: "active", labelKey: "evolution.skill.state.active" },
  { value: "stale", labelKey: "evolution.skill.state.stale" },
  { value: "archived", labelKey: "evolution.skill.state.archived" },
];

const ORIGIN_OPTIONS: { value: SkillOriginFilter; labelKey: string }[] = [
  { value: "all", labelKey: "evolution.skill.filterOriginAll" },
  { value: "bundled", labelKey: "evolution.skill.origin.bundled" },
  {
    value: "user-requested",
    labelKey: "evolution.skill.origin.userRequested",
  },
  { value: "agent-created", labelKey: "evolution.skill.origin.agentCreated" },
];

export function SkillList({
  skills,
  loading = false,
  onTogglePin,
  onFiltersChange,
}: SkillListProps) {
  const { t } = useTranslation();
  const [stateFilter, setStateFilter] = React.useState<SkillStateFilter>("all");
  const [originFilter, setOriginFilter] = React.useState<SkillOriginFilter>(
    "all",
  );
  const [search, setSearch] = React.useState("");

  React.useEffect(() => {
    onFiltersChange?.({ state: stateFilter, origin: originFilter, search });
  }, [stateFilter, originFilter, search, onFiltersChange]);

  const filtered = React.useMemo(() => {
    const needle = search.trim().toLowerCase();
    return skills.filter((s) => {
      if (stateFilter !== "all" && s.state !== stateFilter) return false;
      if (originFilter !== "all" && s.origin !== originFilter) return false;
      if (needle) {
        const hay = `${s.name}\n${s.description}`.toLowerCase();
        if (!hay.includes(needle)) return false;
      }
      return true;
    });
  }, [skills, stateFilter, originFilter, search]);

  return (
    <div className="flex flex-col gap-3">
      {/* Filter row */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative">
          <Search
            aria-hidden
            className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-tp-ink-3"
          />
          <Input
            data-testid="skill-search"
            type="search"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t("evolution.skill.searchPlaceholder")}
            className="w-56 pl-8"
            aria-label={t("evolution.skill.searchPlaceholder")}
          />
        </div>
        <FilterSelect
          testId="skill-filter-state"
          ariaLabel={t("evolution.skill.filterState")}
          value={stateFilter}
          options={STATE_OPTIONS.map((o) => ({
            value: o.value,
            label: t(o.labelKey),
          }))}
          onChange={(v) => setStateFilter(v as SkillStateFilter)}
        />
        <FilterSelect
          testId="skill-filter-origin"
          ariaLabel={t("evolution.skill.filterOrigin")}
          value={originFilter}
          options={ORIGIN_OPTIONS.map((o) => ({
            value: o.value,
            label: t(o.labelKey),
          }))}
          onChange={(v) => setOriginFilter(v as SkillOriginFilter)}
        />
      </div>

      {/* List */}
      {loading ? (
        <div className="py-6 text-center text-sm text-tp-ink-3">
          {t("common.loading")}
        </div>
      ) : filtered.length === 0 ? (
        <div
          data-testid="skill-list-empty"
          className="py-6 text-center text-sm text-tp-ink-3"
        >
          {t("evolution.skill.empty")}
        </div>
      ) : (
        <ul
          data-testid="skill-list"
          className="flex flex-col divide-y divide-tp-glass-edge overflow-hidden rounded-xl border border-tp-glass-edge"
        >
          {filtered.map((s) => (
            <SkillRow key={s.name} skill={s} onTogglePin={onTogglePin} />
          ))}
        </ul>
      )}
    </div>
  );
}

function FilterSelect({
  testId,
  ariaLabel,
  value,
  options,
  onChange,
}: {
  testId: string;
  ariaLabel: string;
  value: string;
  options: { value: string; label: string }[];
  onChange: (next: string) => void;
}) {
  return (
    <select
      data-testid={testId}
      aria-label={ariaLabel}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className={cn(
        "h-8 rounded-md border border-input bg-background px-2 text-xs",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
      )}
    >
      {options.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  );
}

function SkillRow({
  skill,
  onTogglePin,
}: {
  skill: SkillSummary;
  onTogglePin: (name: string, nextPinned: boolean) => void;
}) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = React.useState(false);

  return (
    <li
      data-testid={`skill-row-${skill.name}`}
      className="bg-tp-glass-inner/40"
    >
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
        className="flex w-full items-center gap-3 px-3 py-2 text-left text-[13px] hover:bg-tp-glass-inner-hover"
      >
        <span className="flex min-w-0 flex-1 flex-col gap-1">
          <span className="flex items-center gap-2">
            <span className="font-mono font-semibold text-tp-ink-1">
              {skill.name}
            </span>
            <span
              className="font-mono text-[10.5px] text-tp-ink-3"
              data-testid={`skill-version-${skill.name}`}
            >
              {t("evolution.skill.versionLabel", { v: skill.version })}
            </span>
          </span>
          <span className="flex flex-wrap items-center gap-1.5">
            <SkillBadge kind="state" value={skill.state} />
            <SkillBadge kind="origin" value={skill.origin} />
            <span className="text-[10.5px] text-tp-ink-3">
              {skill.last_used_at
                ? `· ${t("evolution.skill.useCount", { n: skill.use_count })}`
                : `· ${t("evolution.skill.lastUsedNever")}`}
            </span>
          </span>
        </span>
        <span
          onClick={(e) => {
            e.stopPropagation();
            onTogglePin(skill.name, !skill.pinned);
          }}
          role="button"
          tabIndex={0}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              e.stopPropagation();
              onTogglePin(skill.name, !skill.pinned);
            }
          }}
          aria-label={
            skill.pinned
              ? t("evolution.skill.unpin")
              : t("evolution.skill.pin")
          }
          aria-pressed={skill.pinned}
          data-testid={`pin-toggle-${skill.name}`}
          title={
            skill.pinned ? t("evolution.skill.pinnedTooltip") : undefined
          }
          className={cn(
            "inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md",
            "hover:bg-tp-glass-inner-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
            skill.pinned ? "text-amber-600 dark:text-amber-400" : "text-tp-ink-3",
          )}
        >
          {skill.pinned ? (
            <Pin className="h-3.5 w-3.5" />
          ) : (
            <PinOff className="h-3.5 w-3.5" />
          )}
        </span>
      </button>
      {expanded ? (
        <div className="border-t border-tp-glass-edge px-3 py-2 text-[12px] text-tp-ink-2">
          {skill.description || (
            <span className="italic text-tp-ink-3">{t("common.empty")}</span>
          )}
        </div>
      ) : null}
    </li>
  );
}
