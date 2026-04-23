"use client";

import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { motion } from "framer-motion";
import { Plus, Search } from "lucide-react";

import { cn } from "@/lib/utils";
import { useMotionVariants } from "@/lib/motion";
import { fetchAgents, type AgentCard } from "@/lib/mocks/characters";
import { GlassPanel } from "@/components/ui/glass-panel";
import { StatChip } from "@/components/ui/stat-chip";
import {
  FilterChipGroup,
  type FilterChipOption,
} from "@/components/ui/filter-chip-group";
import { useCommandPalette } from "@/components/cmdk-palette";
import { CharacterCard, tiltForName } from "@/components/characters/character-card";
import { CharacterDrawer } from "@/components/characters/character-drawer";

/**
 * Characters — Tidepool (Phase 5c) cutover.
 *
 * Layout:
 *   ┌──────────── hero (glass strong) ────────────┐
 *   │ lead pill · title · prose · [New] [⌘K]     │
 *   └──────────────────────────────────────────────┘
 *   [ StatChip × 4 — total · tagged · tools · skills ]
 *   [ Search ]  [ FilterChipGroup — by tool tag ]
 *   ┌ card grid — minmax(280px,1fr) ─────────────┐
 *   │ <CharacterCard> × N                         │
 *   └──────────────────────────────────────────────┘
 *   [ <CharacterDrawer> — radix modal, preserved ]
 *
 * Data today is a local mock (`fetchAgents`); when the backend ships the
 * real `GET /admin/agents` this fetcher swaps without touching the tree.
 */

const SPARK_TOTAL =
  "M0 28 L30 24 L60 26 L90 20 L120 22 L150 16 L180 18 L210 12 L240 14 L270 8 L300 10 L300 36 L0 36 Z";
const SPARK_TAGGED =
  "M0 22 L30 22 L60 20 L90 22 L120 18 L150 20 L180 18 L210 20 L240 16 L270 18 L300 16 L300 36 L0 36 Z";
const SPARK_TOOLS =
  "M0 28 L30 26 L60 24 L90 22 L120 20 L150 16 L180 14 L210 10 L240 8 L270 6 L300 4 L300 36 L0 36 Z";
const SPARK_SKILLS =
  "M0 30 L30 28 L60 24 L90 22 L120 20 L150 18 L180 16 L210 14 L240 12 L270 10 L300 8 L300 36 L0 36 Z";

export default function CharactersPage() {
  const { t } = useTranslation();
  const variants = useMotionVariants();
  const query = useQuery<AgentCard[]>({
    queryKey: ["admin", "agents", "characters"],
    queryFn: fetchAgents,
    retry: false,
  });

  const [search, setSearch] = React.useState("");
  const [tagFilter, setTagFilter] = React.useState<string>("all");
  // `null` → closed. `"__new__"` → create mode. Otherwise the character name.
  const [drawerTarget, setDrawerTarget] = React.useState<string | null>(null);

  const cards = React.useMemo<AgentCard[]>(
    () => query.data ?? [],
    [query.data],
  );
  const offline = query.isError;

  const byName = React.useMemo(() => {
    const map = new Map<string, AgentCard>();
    for (const c of cards) map.set(c.name, c);
    return map;
  }, [cards]);

  const counts = React.useMemo(() => {
    const c = { total: cards.length, tagged: 0, tools: 0, skills: 0 };
    for (const card of cards) {
      if (card.tools_allowed.length > 0) {
        c.tools += 1;
        c.tagged += 1;
      } else if (card.skill_refs.length > 0) {
        c.tagged += 1;
      }
      if (card.skill_refs.length > 0) c.skills += 1;
    }
    return c;
  }, [cards]);

  // Derive the tag vocabulary from every character's tool list so filter
  // chips stay in sync with the data. Capped to the top 8 most-common tools
  // to keep the chip group from overflowing.
  const topTags = React.useMemo(() => {
    const freq = new Map<string, number>();
    for (const c of cards) {
      for (const tool of c.tools_allowed) {
        freq.set(tool, (freq.get(tool) ?? 0) + 1);
      }
    }
    return [...freq.entries()]
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
      .slice(0, 8)
      .map(([tag, count]) => ({ tag, count }));
  }, [cards]);

  const filtered = React.useMemo(() => {
    const q = search.trim().toLowerCase();
    return cards.filter((c) => {
      if (tagFilter !== "all" && !c.tools_allowed.includes(tagFilter)) {
        return false;
      }
      if (!q) return true;
      return (
        c.name.toLowerCase().includes(q) ||
        c.description.toLowerCase().includes(q)
      );
    });
  }, [cards, search, tagFilter]);

  const updatedLabel = React.useMemo(() => {
    const dataUpdatedAt = query.dataUpdatedAt;
    if (!dataUpdatedAt) return undefined;
    return formatRelative(new Date(dataUpdatedAt).toISOString(), t);
  }, [query.dataUpdatedAt, t]);

  const filterOptions: FilterChipOption[] = [
    { value: "all", label: t("characters.tp.filterAll"), count: counts.total },
    ...topTags.map(({ tag, count }) => ({
      value: tag,
      label: tag,
      count,
      tone: "neutral" as const,
    })),
  ];

  const drawerCard: AgentCard | null =
    drawerTarget && drawerTarget !== "__new__"
      ? (byName.get(drawerTarget) ?? null)
      : null;

  function onDrawerOpenChange(open: boolean) {
    if (!open) setDrawerTarget(null);
  }

  return (
    <motion.div
      className="flex flex-col gap-4"
      variants={variants.fadeUp}
      initial="hidden"
      animate="visible"
    >
      <CharactersHero
        counts={offline ? undefined : counts}
        updatedLabel={updatedLabel}
        offline={offline}
        onNew={() => setDrawerTarget("__new__")}
      />

      {/* Stat chips row */}
      <motion.section
        className="grid grid-cols-1 gap-3.5 md:grid-cols-2 xl:grid-cols-4"
        variants={variants.stagger}
        initial="hidden"
        animate="visible"
      >
        <StatChip
          variant="primary"
          live={!offline}
          label={t("characters.tp.statTotal")}
          value={offline ? "—" : counts.total}
          foot={
            offline
              ? t("dashboard.endpointOffline")
              : t("characters.tp.statFootTotal")
          }
          sparkPath={SPARK_TOTAL}
          sparkTone="amber"
        />
        <StatChip
          label={t("characters.tp.statTagged")}
          value={offline ? "—" : counts.tagged}
          delta={
            !offline && counts.total > 0
              ? {
                  label: `${counts.tagged} / ${counts.total}`,
                  tone: counts.tagged === counts.total ? "up" : "flat",
                }
              : undefined
          }
          foot={
            offline
              ? t("dashboard.endpointOffline")
              : t("characters.tp.statFootTagged")
          }
          sparkPath={SPARK_TAGGED}
          sparkTone="ember"
        />
        <StatChip
          label={t("characters.tp.statWithTools")}
          value={offline ? "—" : counts.tools}
          foot={
            offline
              ? t("dashboard.endpointOffline")
              : t("characters.tp.statFootWithTools")
          }
          sparkPath={SPARK_TOOLS}
          sparkTone="peach"
        />
        <StatChip
          label={t("characters.tp.statWithSkills")}
          value={offline ? "—" : counts.skills}
          foot={
            offline
              ? t("dashboard.endpointOffline")
              : t("characters.tp.statFootWithSkills")
          }
          sparkPath={SPARK_SKILLS}
          sparkTone="ember"
        />
      </motion.section>

      {/* Search + tag filter chips */}
      <section className="flex flex-wrap items-center justify-between gap-3">
        <label className="relative flex min-w-[220px] flex-1 items-center sm:max-w-[360px]">
          <Search
            className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-tp-ink-4"
            aria-hidden
          />
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t("characters.tp.searchPlaceholder")}
            aria-label={t("characters.tp.searchPlaceholder")}
            className={cn(
              "h-9 w-full rounded-lg border pl-8 pr-3 text-[13px]",
              "border-tp-glass-edge bg-tp-glass-inner text-tp-ink",
              "placeholder:text-tp-ink-4 transition-colors",
              "hover:bg-tp-glass-inner-hover",
              "focus:outline-none focus:ring-2 focus:ring-tp-amber/40",
            )}
          />
        </label>
        {filterOptions.length > 1 ? (
          <FilterChipGroup
            options={filterOptions}
            value={tagFilter}
            onChange={(next) => setTagFilter(next)}
            label={t("characters.tp.filterLabel")}
          />
        ) : null}
      </section>

      {/* Card grid / empty / loading / error */}
      {query.isPending ? (
        <CardGridSkeleton />
      ) : offline ? (
        <OfflineBlock message={(query.error as Error | undefined)?.message} />
      ) : filtered.length === 0 ? (
        <EmptyBlock hasAny={cards.length > 0} />
      ) : (
        <motion.section
          aria-label={t("characters.title")}
          variants={variants.stagger}
          initial="hidden"
          animate="visible"
          className={cn(
            "grid gap-3",
            "grid-cols-[repeat(auto-fill,minmax(280px,1fr))]",
          )}
          data-testid="characters-grid"
        >
          {filtered.map((card) => (
            <motion.div key={card.name} variants={variants.listItem}>
              <CharacterCard
                card={card}
                rotateDeg={tiltForName(card.name)}
                onOpen={() => setDrawerTarget(card.name)}
                onEdit={() => setDrawerTarget(card.name)}
              />
            </motion.div>
          ))}
        </motion.section>
      )}

      <CharacterDrawer
        open={drawerTarget !== null}
        onOpenChange={onDrawerOpenChange}
        card={drawerCard}
      />
    </motion.div>
  );
}

// ─── Sub-components ────────────────────────────────────────────────────

interface HeroCounts {
  total: number;
  tagged: number;
  tools: number;
  skills: number;
}

function CharactersHero({
  counts,
  updatedLabel,
  offline,
  onNew,
}: {
  counts: HeroCounts | undefined;
  updatedLabel: string | undefined;
  offline: boolean;
  onNew: () => void;
}) {
  const { t } = useTranslation();
  const palette = useCommandPalette();

  const total = counts?.total ?? 0;
  const tagged = counts?.tagged ?? 0;
  const tools = counts?.tools ?? 0;
  const skills = counts?.skills ?? 0;

  return (
    <GlassPanel variant="strong" as="section" className="relative overflow-hidden p-7">
      {/* Ambient amber/ember glow behind the copy. */}
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
        {/* Lead pill */}
        <div
          className={cn(
            "inline-flex w-fit items-center gap-2.5 rounded-full border py-1 pl-2 pr-3",
            "border-tp-glass-edge bg-tp-glass-inner-strong",
            "font-mono text-[11px] text-tp-ink-2",
          )}
        >
          <span
            className={cn(
              "h-1.5 w-1.5 rounded-full",
              offline ? "bg-tp-err" : "bg-tp-amber",
            )}
          />
          {offline
            ? t("characters.tp.leadPillOffline")
            : t("characters.tp.leadPill", { total, tagged })}
        </div>

        <h1 className="text-balance font-sans text-[28px] font-semibold leading-[1.15] tracking-[-0.025em] text-tp-ink sm:text-[32px]">
          {t("characters.title")}
        </h1>

        <p className="max-w-[72ch] text-[14.5px] leading-[1.6] text-tp-ink-2">
          {offline ? (
            <>{t("characters.tp.proseOffline")}</>
          ) : (
            <>
              {t("characters.tp.proseLead", { total })}
              {t("characters.tp.proseMiddle", {
                withTools: tools,
                withSkills: skills,
              })}
              {t("characters.tp.proseTail")}
              {updatedLabel
                ? ` ${t("characters.tp.proseUpdated", { when: updatedLabel })}`
                : ""}
            </>
          )}
        </p>

        <div className="mt-1 flex flex-wrap items-center gap-2.5">
          <button
            type="button"
            onClick={onNew}
            aria-label={t("characters.tp.ctaNewAria")}
            data-testid="character-new"
            className={cn(
              "inline-flex items-center gap-2 rounded-lg border px-3 py-2 text-[13px] font-medium",
              "border-tp-amber/35 bg-tp-amber-soft text-tp-amber",
              "transition-colors hover:bg-[color-mix(in_oklch,var(--tp-amber)_22%,transparent)]",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/50",
            )}
          >
            <Plus className="h-3.5 w-3.5" aria-hidden />
            {t("characters.tp.ctaNew")}
          </button>

          <button
            type="button"
            onClick={() => palette.setOpen(true)}
            className={cn(
              "inline-flex items-center gap-2 rounded-lg border px-3 py-2 text-[13px] font-medium",
              "border-tp-glass-edge bg-tp-glass-inner text-tp-ink-2",
              "transition-colors hover:bg-tp-glass-inner-hover hover:text-tp-ink",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/40",
            )}
          >
            <Search className="h-3.5 w-3.5" aria-hidden />
            {t("characters.tp.ctaPaletteHint")}
            <span className="ml-1 rounded bg-black/5 px-1.5 py-0.5 font-mono text-[10px] text-tp-ink-3 dark:bg-white/5">
              ⌘K
            </span>
          </button>
        </div>
      </div>
    </GlassPanel>
  );
}

function CardGridSkeleton() {
  return (
    <section
      aria-hidden
      className="grid gap-3 grid-cols-[repeat(auto-fill,minmax(280px,1fr))]"
    >
      {Array.from({ length: 6 }).map((_, i) => (
        <GlassPanel
          key={i}
          variant="soft"
          className="flex h-[132px] flex-col gap-3 p-4"
        >
          <div className="flex items-center gap-3">
            <div className="h-8 w-8 rounded-full bg-tp-glass-inner-strong" />
            <div className="flex-1 space-y-1.5">
              <div className="h-3.5 w-2/3 rounded bg-tp-glass-inner-strong" />
              <div className="h-3 w-1/2 rounded bg-tp-glass-inner" />
            </div>
          </div>
          <div className="mt-auto flex gap-2">
            <div className="h-5 w-14 rounded-full bg-tp-glass-inner" />
            <div className="h-5 w-16 rounded-full bg-tp-glass-inner" />
          </div>
        </GlassPanel>
      ))}
    </section>
  );
}

function OfflineBlock({ message }: { message?: string }) {
  const { t } = useTranslation();
  // Cap runaway error bodies (e.g. an HTML 404 page) to a single tidy line.
  const firstLine = message
    ?.split(/\r?\n/)
    .find((ln) => ln.trim().length > 0)
    ?.trim();
  const short =
    firstLine && firstLine.length > 180
      ? firstLine.slice(0, 180) + "…"
      : firstLine;
  return (
    <GlassPanel variant="soft" className="flex flex-col items-center gap-2 p-8 text-center">
      <div className="font-mono text-[11px] uppercase tracking-[0.12em] text-tp-err">
        {t("characters.tp.offlineTitle")}
      </div>
      <p className="max-w-prose text-[13px] text-tp-ink-2">
        {t("characters.tp.offlineHint")}
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

function EmptyBlock({ hasAny }: { hasAny: boolean }) {
  const { t } = useTranslation();
  return (
    <GlassPanel variant="soft" className="flex flex-col items-center gap-2 p-8 text-center">
      <div className="text-[14px] font-medium text-tp-ink">
        {hasAny ? t("characters.tp.emptyTitle") : t("characters.tp.emptyNone")}
      </div>
      {hasAny ? (
        <p className="text-[13px] text-tp-ink-3">
          {t("characters.tp.emptyHint")}
        </p>
      ) : null}
    </GlassPanel>
  );
}

// ─── Helpers ────────────────────────────────────────────────────────────

function formatRelative(
  iso: string,
  t: (key: string, opts?: Record<string, unknown>) => string,
): string {
  try {
    const then = new Date(iso).getTime();
    const now = Date.now();
    const s = Math.round((now - then) / 1000);
    if (s < 60) return t("common.secondsAgo", { n: Math.max(s, 0) });
    if (s < 3600) return t("common.minutesAgo", { n: Math.round(s / 60) });
    if (s < 86400) return t("common.hoursAgo", { n: Math.round(s / 3600) });
    return t("common.daysAgo", { n: Math.round(s / 86400) });
  } catch {
    return iso;
  }
}
