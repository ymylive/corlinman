/**
 * Tag Memo Dashboard — Tidepool (Phase 5e) retoken.
 *
 * Three linked visualisations over the EPA / Residual-Pyramid pipeline:
 *   ① scatter  — first 2 projections, depth by colour (amber→ember),
 *                energy by radius.
 *   ② dual-line — entropy (amber) + logic_depth (ember), drawn in over 1.2s.
 *   ③ residual pyramid — one row per chunk, pyramid levels as warm-ramp
 *                        stacked segments.
 *
 * Hovering a mark in any panel lights up the same chunk in the other two
 * (via `HoveredIdProvider`). Mock data today; real endpoint from
 * `corlinman-tagmemo` (B3-BE4) will slot in behind the same component API.
 *
 * Tidepool primitives in use: `GlassPanel` (strong), `StatChip` (first one
 * primary + live), `AnimatedNumber` for the chunk count.
 */
"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import { motion } from "framer-motion";

import { cn } from "@/lib/utils";
import { AnimatedNumber } from "@/components/ui/animated-number";
import { GlassPanel } from "@/components/ui/glass-panel";
import { StatChip } from "@/components/ui/stat-chip";
import { useMotion } from "@/components/ui/motion-safe";
import { EpaScatter } from "@/components/viz/epa-scatter";
import { DualLine } from "@/components/viz/dual-line";
import { ResidualPyramid } from "@/components/viz/residual-pyramid";
import { HoveredIdProvider } from "@/components/viz/use-hovered-id";
import {
  generateMockChunks,
  summariseChunks,
  type TagMemoChunk,
} from "@/lib/mocks/tagmemo";

// TODO(B3-BE4): swap mock for apiFetch<TagMemoChunk[]>("/admin/tagmemo/chunks")
export default function TagMemoPage() {
  const { t } = useTranslation();
  const { reduced } = useMotion();

  const chunks = React.useMemo<TagMemoChunk[]>(
    () => generateMockChunks(),
    [],
  );
  const stats = React.useMemo(() => summariseChunks(chunks), [chunks]);

  const [parentWidth, setParentWidth] = React.useState(900);
  const pyramidRef = React.useRef<HTMLDivElement>(null);
  React.useEffect(() => {
    if (!pyramidRef.current) return;
    const el = pyramidRef.current;
    const update = () => setParentWidth(el.getBoundingClientRect().width);
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const pctFmt = React.useMemo(
    () =>
      new Intl.NumberFormat(undefined, {
        style: "percent",
        maximumFractionDigits: 1,
      }),
    [],
  );

  return (
    <HoveredIdProvider>
      <motion.div
        className="flex flex-col gap-5"
        initial={reduced ? undefined : { opacity: 0, y: 6 }}
        animate={reduced ? undefined : { opacity: 1, y: 0 }}
        transition={{ duration: 0.28, ease: [0.16, 1, 0.3, 1] }}
      >
        <TagMemoHero
          chunkCount={stats.chunkCount}
          avgEntropyLabel={pctFmt.format(stats.avgEntropy)}
          avgLogicDepthLabel={pctFmt.format(stats.avgLogicDepth)}
        />

        <section
          className="grid grid-cols-2 gap-3.5 md:grid-cols-4"
          aria-label="Tag memo stats"
        >
          <StatChip
            variant="primary"
            live
            label={t("tagmemo.tp.statChunks")}
            value={<AnimatedNumber value={stats.chunkCount} format="number" />}
            foot={t("tagmemo.tp.statChunksFoot")}
            sparkPath={CHUNKS_SPARK}
            sparkTone="amber"
            data-testid="tagmemo-stat-chunks"
          />
          <StatChip
            label={t("tagmemo.tp.statAvgEntropy")}
            value={
              <AnimatedNumber
                value={stats.avgEntropy}
                format="percent"
                formatOptions={{ maximumFractionDigits: 1 }}
              />
            }
            foot={t("tagmemo.tp.statAvgEntropyFoot")}
            sparkPath={ENTROPY_SPARK}
            sparkTone="peach"
            data-testid="tagmemo-stat-entropy"
          />
          <StatChip
            label={t("tagmemo.tp.statAvgLogicDepth")}
            value={
              <AnimatedNumber
                value={stats.avgLogicDepth}
                format="percent"
                formatOptions={{ maximumFractionDigits: 1 }}
              />
            }
            foot={t("tagmemo.tp.statAvgLogicDepthFoot")}
            sparkPath={DEPTH_SPARK}
            sparkTone="ember"
            data-testid="tagmemo-stat-logic-depth"
          />
          <StatChip
            label={t("tagmemo.tp.statUniqueAxes")}
            value={<AnimatedNumber value={stats.uniqueAxes} format="number" />}
            foot={t("tagmemo.tp.statUniqueAxesFoot")}
            sparkPath={AXES_SPARK}
            sparkTone="ember"
            data-testid="tagmemo-stat-axes"
          />
        </section>

        <section className="grid grid-cols-1 gap-4 xl:grid-cols-2">
          <VizPanel
            title={t("tagmemo.tp.panelScatterTitle")}
            meta={t("tagmemo.tp.panelScatterMeta")}
            testid="panel-scatter"
          >
            <EpaScatter chunks={chunks} />
          </VizPanel>
          <VizPanel
            title={t("tagmemo.tp.panelDualLineTitle")}
            meta={t("tagmemo.tp.panelDualLineMeta")}
            testid="panel-dual-line"
          >
            <DualLine chunks={chunks} />
          </VizPanel>
        </section>

        <section ref={pyramidRef} data-testid="panel-pyramid">
          <VizPanel
            title={t("tagmemo.tp.panelPyramidTitle")}
            meta={t("tagmemo.tp.panelPyramidMeta")}
            testid="panel-pyramid-inner"
          >
            <ResidualPyramid chunks={chunks} parentWidth={parentWidth} />
          </VizPanel>
        </section>

        {/* Screen-reader / no-JS fallback. Present in DOM; visually hidden
            for sighted users (the panels above carry the same data). */}
        <details className="sr-only">
          <summary>{t("tagmemo.tp.a11yTableSummary")}</summary>
          <table data-testid="fallback-table">
            <thead>
              <tr>
                <th>{t("tagmemo.tp.a11yColChunk")}</th>
                <th>{t("tagmemo.tp.a11yColEntropy")}</th>
                <th>{t("tagmemo.tp.a11yColLogicDepth")}</th>
                <th>{t("tagmemo.tp.a11yColTopAxis")}</th>
              </tr>
            </thead>
            <tbody>
              {chunks.map((c) => (
                <tr
                  key={c.chunk_id}
                  data-testid={`fallback-row-${c.chunk_id}`}
                >
                  <td>{c.chunk_id}</td>
                  <td>{c.entropy.toFixed(3)}</td>
                  <td>{c.logic_depth.toFixed(3)}</td>
                  <td>{c.dominant_axes[0]?.label ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      </motion.div>
    </HoveredIdProvider>
  );
}

// ---------------------------------------------------------------------------
// Hero — quiet prose pattern (no glass container) mirroring Nodes / Dashboard.
// ---------------------------------------------------------------------------

interface TagMemoHeroProps {
  chunkCount: number;
  avgEntropyLabel: string;
  avgLogicDepthLabel: string;
}

function TagMemoHero({
  chunkCount,
  avgEntropyLabel,
  avgLogicDepthLabel,
}: TagMemoHeroProps) {
  const { t } = useTranslation();
  return (
    <header className="flex flex-col gap-3">
      <h1
        className={cn(
          "font-sans text-[30px] font-semibold leading-[1.12] tracking-[-0.025em] text-tp-ink",
          "sm:text-[34px]",
        )}
      >
        {t("tagmemo.tp.heroTitle")}
      </h1>
      <p className="max-w-[72ch] text-[14px] leading-[1.6] text-tp-ink-2">
        <InlineMetric>
          {t("tagmemo.tp.heroLead", {
            n: chunkCount,
            entropy: avgEntropyLabel,
            depth: avgLogicDepthLabel,
          })}
        </InlineMetric>
        <span className="ml-1 text-tp-ink-2">
          {t("tagmemo.tp.heroSub")}
        </span>
      </p>
    </header>
  );
}

function InlineMetric({ children }: { children: React.ReactNode }) {
  return (
    <span
      className={cn(
        "whitespace-nowrap rounded-md border border-tp-glass-edge bg-tp-glass-inner-strong",
        "px-1.5 py-px font-mono text-[12.5px] font-medium tabular-nums text-tp-ink",
      )}
    >
      {children}
    </span>
  );
}

// ---------------------------------------------------------------------------
// VizPanel — GlassPanel `strong` wrapper with a quiet header row.
// ---------------------------------------------------------------------------

interface VizPanelProps {
  title: string;
  meta: string;
  testid: string;
  children: React.ReactNode;
}

function VizPanel({ title, meta, testid, children }: VizPanelProps) {
  return (
    <GlassPanel
      variant="strong"
      className="relative overflow-hidden p-4"
      data-testid={testid}
    >
      {/* Warm ambient glow anchored to the top-right so the viz reads as
          "lit from above" without fighting the amber strokes. */}
      <div
        aria-hidden
        className="pointer-events-none absolute -right-12 -top-12 h-[180px] w-[260px] rounded-full opacity-60 blur-3xl"
        style={{
          background:
            "radial-gradient(closest-side, var(--tp-amber-glow), transparent 70%)",
        }}
      />
      <div className="relative">
        <header className="mb-3 flex items-center justify-between gap-2 px-1">
          <div className="font-mono text-[11px] uppercase tracking-[0.12em] text-tp-ink-2">
            {title}
          </div>
          <div className="truncate font-mono text-[10.5px] text-tp-ink-4">
            {meta}
          </div>
        </header>
        {children}
      </div>
    </GlassPanel>
  );
}

// ---------------------------------------------------------------------------
// Ambient sparkline paths — same geometry as the nodes/skills/scheduler
// pages so the tag-memo dashboard reads as one voice.
// ---------------------------------------------------------------------------

const CHUNKS_SPARK =
  "M0 24 L30 22 L60 20 L90 22 L120 18 L150 16 L180 18 L210 14 L240 16 L270 12 L300 14 L300 36 L0 36 Z";
const ENTROPY_SPARK =
  "M0 26 L30 24 L60 20 L90 22 L120 18 L150 20 L180 16 L210 18 L240 14 L270 16 L300 12 L300 36 L0 36 Z";
const DEPTH_SPARK =
  "M0 28 L30 26 L60 24 L90 20 L120 22 L150 18 L180 20 L210 16 L240 18 L270 14 L300 16 L300 36 L0 36 Z";
const AXES_SPARK =
  "M0 30 L30 28 L60 28 L90 26 L120 26 L150 24 L180 24 L210 22 L240 22 L270 20 L300 20 L300 36 L0 36 Z";
