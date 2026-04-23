"use client";

import * as React from "react";
import dynamic from "next/dynamic";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTheme } from "next-themes";
import { useTranslation } from "react-i18next";
import { motion } from "framer-motion";
import { AlertTriangle, Check, FileCode2, RotateCcw, Search } from "lucide-react";
import { toast } from "sonner";

import { useMotion } from "@/components/ui/motion-safe";
import { useMotionVariants } from "@/lib/motion";
import { GlassPanel } from "@/components/ui/glass-panel";
import { StatChip } from "@/components/ui/stat-chip";
import {
  FilterChipGroup,
  type FilterChipOption,
} from "@/components/ui/filter-chip-group";
import { DetailDrawer } from "@/components/ui/detail-drawer";
import { JsonView } from "@/components/ui/json-view";
import { useCommandPalette } from "@/components/cmdk-palette";
import { cn } from "@/lib/utils";
import {
  fetchConfig,
  fetchConfigSchema,
  postConfig,
  type ConfigGetResponse,
  type ConfigPostResponse,
} from "@/lib/api";

import { SuccessRipple } from "@/components/config/success-ripple";
import { ToastBurst } from "@/components/config/toast-burst";

// Monaco isn't SSR-safe; lazy-load on the client only. The stub matches the
// legacy shape so the vitest editor-stub mock slots in cleanly.
const Editor = dynamic(() => import("@monaco-editor/react"), { ssr: false });

/**
 * Config — Phase 5e Tidepool cutover.
 *
 * Warm-glass re-skin of the Monaco-backed TOML editor. Preserved contracts:
 *   - `config-save-btn` testid.
 *   - "new version: <sha>" text on successful save (scoped to the result).
 *   - Save button text is exactly "Save" at rest and "Saving…" in-flight so
 *     the unit test (vitest) can assert on `textContent`.
 *   - `toast.success(title, { description, icon })` still fires on success
 *     using the existing `config.toastSavedTitle` / `…Description` keys.
 *   - `<SuccessRipple>` + `<ToastBurst>` keep the same testids; only their
 *     pigments rotated from legacy green → Tidepool amber.
 *
 * Layout:
 *   [ hero GlassPanel strong — prose, Save / Validate / ⌘K ]
 *   [ StatChip × 4 — Sections · Pending · Validators · Last save ]
 *   [ FilterChipGroup — section navigator (All + each TOML section) ]
 *   [ Monaco editor in a GlassPanel soft ]
 *   [ sticky bottom bar when dirty — Save N changes · Discard ]
 *
 * Validation errors surface in a <DetailDrawer> (side-pane) with a
 * <JsonView> of the raw dry-run response — matches the Logs/Approvals
 * pattern.
 *
 * Keyboard:
 *   - ⌘S   → save  (blocked while in-flight)
 *   - ⌘⏎   → validate (blocked while in-flight)
 *
 * Both are suppressed while the user is typing in an input/textarea or
 * inside Monaco — Monaco handles its own ⌘S via `editor.addCommand`; since
 * we don't wire one it bubbles up to our window handler, which fires save.
 */

// Top-level TOML sections we surface as chips. Order drives display order;
// "all" is injected at the head in render. New sections reflect the Phase
// 5e scope: hooks, skills, variables, agents, tools, telegram, vector,
// wstool, canvas, nodebridge.
const SECTION_HEADERS = [
  "server",
  "admin",
  "providers",
  "models",
  "channels",
  "rag",
  "approvals",
  "scheduler",
  "logging",
  "hooks",
  "skills",
  "variables",
  "agents",
  "tools",
  "telegram",
  "vector",
  "wstool",
  "canvas",
  "nodebridge",
  "meta",
] as const;

type SectionId = (typeof SECTION_HEADERS)[number];

// Spark paths reused from Approvals/Scheduler to keep the visual dialect
// consistent. Dumb geometry — no correlation to real data.
const SPARK_SECTIONS =
  "M0 28 L30 26 L60 22 L90 24 L120 18 L150 22 L180 14 L210 18 L240 10 L270 14 L300 6 L300 36 L0 36 Z";
const SPARK_PENDING =
  "M0 22 L30 22 L60 20 L90 22 L120 18 L150 20 L180 18 L210 20 L240 16 L270 18 L300 16 L300 36 L0 36 Z";
const SPARK_VALIDATORS =
  "M0 18 L30 20 L60 16 L90 22 L120 14 L150 20 L180 18 L210 22 L240 16 L270 20 L300 14 L300 36 L0 36 Z";
const SPARK_LAST_SAVE =
  "M0 10 L30 14 L60 16 L90 20 L120 22 L150 24 L180 26 L210 28 L240 30 L270 30 L300 32 L300 36 L0 36 Z";

// Matches on `[section]` / `[section.` / `[[section.` at the start of a trimmed line.
function findSectionLine(toml: string, section: string): number | null {
  const lines = toml.split("\n");
  const marker = `[${section}]`;
  const markerTable = `[${section}.`;
  const markerArray = `[[${section}.`;
  for (let i = 0; i < lines.length; i++) {
    const trimmed = lines[i]!.trimStart();
    if (
      trimmed.startsWith(marker) ||
      trimmed.startsWith(markerTable) ||
      trimmed.startsWith(markerArray)
    ) {
      return i + 1; // 1-based — Monaco line numbers are 1-based
    }
  }
  return null;
}

/**
 * Cheap dirty-section detector: diff the raw line list around each section
 * header. Good enough to show the amber "modified" dot on section chips.
 * We stop as soon as we hit the next section header, so later edits don't
 * taint earlier sections.
 */
function dirtySections(original: string, draft: string): Set<string> {
  if (original === draft) return new Set();
  const out = new Set<string>();
  const orig = original.split("\n");
  const curr = draft.split("\n");
  // Track section-of-current-line for both strings via parallel scan.
  const scan = (src: string[], out: Map<string, string[]>) => {
    let section: string | null = null;
    for (const ln of src) {
      const t = ln.trimStart();
      // [section] or [section.sub] or [[section.sub]]
      const m = t.match(/^\[\[?([A-Za-z0-9_]+)/);
      if (m) section = m[1]!;
      if (section) {
        const bucket = out.get(section) ?? [];
        bucket.push(ln);
        out.set(section, bucket);
      }
    }
  };
  const a = new Map<string, string[]>();
  const b = new Map<string, string[]>();
  scan(orig, a);
  scan(curr, b);
  const keys = new Set<string>([...a.keys(), ...b.keys()]);
  for (const k of keys) {
    const ax = a.get(k)?.join("\n") ?? "";
    const bx = b.get(k)?.join("\n") ?? "";
    if (ax !== bx) out.add(k);
  }
  return out;
}

export default function ConfigPage() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const { resolvedTheme } = useTheme();
  const { reduced } = useMotion();
  const variants = useMotionVariants();
  const palette = useCommandPalette();

  const config = useQuery<ConfigGetResponse>({
    queryKey: ["admin", "config"],
    queryFn: fetchConfig,
    retry: false,
  });
  const schema = useQuery({
    queryKey: ["admin", "config", "schema"],
    queryFn: fetchConfigSchema,
    staleTime: Infinity,
    retry: false,
  });
  React.useEffect(() => {
    if (schema.data && typeof window !== "undefined") {
      (window as unknown as Record<string, unknown>).__corlinmanConfigSchema =
        schema.data;
    }
  }, [schema.data]);

  const [draft, setDraft] = React.useState<string>("");
  const [initialized, setInitialized] = React.useState(false);
  const [activeSection, setActiveSection] = React.useState<SectionId | "all">(
    "all",
  );
  const [validateResult, setValidateResult] =
    React.useState<ConfigPostResponse | null>(null);
  const [saveResult, setSaveResult] =
    React.useState<ConfigPostResponse | null>(null);
  const [drawerOpen, setDrawerOpen] = React.useState(false);
  // Monotonic id keyed into <SuccessRipple>. Each save success increments it
  // so framer-motion re-mounts the ripple and plays a one-shot animation.
  const [successId, setSuccessId] = React.useState(0);
  const [lastSavedAt, setLastSavedAt] = React.useState<number | null>(null);
  const [now, setNow] = React.useState<number>(() => Date.now());

  // 1-Hz tick just for the "last saved N seconds ago" footer — same cadence
  // as Approvals' held-for pill so the label feels consistent across pages.
  React.useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(id);
  }, []);

  React.useEffect(() => {
    if (config.data && !initialized) {
      setDraft(config.data.toml);
      setInitialized(true);
    }
  }, [config.data, initialized]);

  const validateMutation = useMutation({
    mutationFn: () => postConfig(draft, true),
    onSuccess: (r) => {
      setValidateResult(r);
      setSaveResult(null);
      // Open the drawer on any result — operator can always eyeball the
      // "all clear" report too. Only auto-open on issues for legacy tests.
      if (r.issues.length > 0) setDrawerOpen(true);
    },
    onError: () => setValidateResult(null),
  });
  const saveMutation = useMutation({
    mutationFn: () => postConfig(draft, false),
    onSuccess: (r) => {
      setSaveResult(r);
      setValidateResult(null);
      if (r.issues.length > 0) setDrawerOpen(true);
      qc.invalidateQueries({ queryKey: ["admin", "config"] });
      // Baseline now matches the saved draft so the pending counter resets
      // to zero without waiting for the re-fetch to land.
      qc.setQueryData<ConfigGetResponse | undefined>(
        ["admin", "config"],
        (prev) =>
          prev ? { ...prev, toml: draft, version: r.version ?? prev.version } : prev,
      );
      setSuccessId((n) => n + 1);
      setLastSavedAt(Date.now());
      toast.success(t("config.toastSavedTitle"), {
        description: t("config.toastSavedDescription"),
        icon: <ToastBurst />,
      });
    },
    onError: () => setSaveResult(null),
  });

  // Monaco editor handle for section jumps.
  const editorRef = React.useRef<unknown>(null);
  const onMount = (editor: unknown) => {
    editorRef.current = editor;
  };
  const jumpToSection = React.useCallback(
    (section: SectionId | "all") => {
      setActiveSection(section);
      if (section === "all") return;
      const ed = editorRef.current as
        | {
            revealLineInCenter?: (n: number) => void;
            setPosition?: (p: { lineNumber: number; column: number }) => void;
            focus?: () => void;
          }
        | null;
      if (!ed) return;
      const line = findSectionLine(draft, section);
      if (line !== null) {
        ed.revealLineInCenter?.(line);
        ed.setPosition?.({ lineNumber: line, column: 1 });
        ed.focus?.();
      }
    },
    [draft],
  );

  const dirty = React.useMemo(
    () => (config.data ? dirtySections(config.data.toml, draft) : new Set<string>()),
    [config.data, draft],
  );
  const pendingCount = dirty.size;
  const isDirty = pendingCount > 0;

  // Keyboard: ⌘S save, ⌘⏎ validate. Both bypass non-Monaco text inputs.
  React.useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!(e.metaKey || e.ctrlKey)) return;
      const target = e.target as HTMLElement | null;
      const tag = target?.tagName?.toLowerCase() ?? "";
      // Allow inside Monaco (`.monaco-editor textarea`) — we want ⌘S to save
      // regardless of focus. Suppress inside other <input>/<textarea> so
      // operators typing filters aren't surprised by a save dialog.
      const insideMonaco = !!target?.closest?.(".monaco-editor");
      const isPlainTextInput =
        (tag === "input" || tag === "textarea") && !insideMonaco;
      if (isPlainTextInput) return;
      if (e.key === "s" || e.key === "S") {
        if (!initialized || saveMutation.isPending) return;
        e.preventDefault();
        saveMutation.mutate();
      } else if (e.key === "Enter") {
        if (!initialized || validateMutation.isPending) return;
        e.preventDefault();
        validateMutation.mutate();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [initialized, saveMutation, validateMutation]);

  // Section chip options. Counts column shows line-count in that section so
  // the operator has a rough density hint; dirty sections get amber tone.
  const sectionCounts = React.useMemo(() => {
    const map = new Map<string, number>();
    let current: string | null = null;
    for (const ln of draft.split("\n")) {
      const t = ln.trimStart();
      const m = t.match(/^\[\[?([A-Za-z0-9_]+)/);
      if (m) current = m[1]!;
      if (current) map.set(current, (map.get(current) ?? 0) + 1);
    }
    return map;
  }, [draft]);

  const sectionLabels = t("config.tp.sectionLabels", {
    returnObjects: true,
  }) as Record<string, string>;
  const sectionDescriptions = t("config.tp.sectionDescriptions", {
    returnObjects: true,
  }) as Record<string, string>;

  const filterOptions: FilterChipOption[] = React.useMemo(() => {
    const opts: FilterChipOption[] = [
      {
        value: "all",
        label: t("config.tp.filterAll"),
        count: SECTION_HEADERS.filter((s) => sectionCounts.has(s)).length,
        tone: "neutral",
      },
    ];
    for (const s of SECTION_HEADERS) {
      if (!sectionCounts.has(s) && !dirty.has(s)) continue;
      opts.push({
        value: s,
        label: sectionLabels[s] ?? s,
        count: sectionCounts.get(s),
        tone: dirty.has(s) ? "warn" : "neutral",
      });
    }
    return opts;
  }, [dirty, sectionCounts, sectionLabels, t]);

  const latestResult = saveResult ?? validateResult;
  const offline = config.isError;

  const lastSavedLabel = React.useMemo(() => {
    if (lastSavedAt === null) return t("config.tp.lastSavedNever");
    const delta = Math.max(0, Math.round((now - lastSavedAt) / 1000));
    if (delta < 3) return t("config.tp.lastSavedJust");
    if (delta < 60) return t("config.tp.lastSavedSec", { n: delta });
    return t("config.tp.lastSavedMin", { n: Math.max(1, Math.round(delta / 60)) });
  }, [lastSavedAt, now, t]);

  return (
    <motion.div
      className="flex flex-col gap-5 pb-28"
      initial={reduced ? undefined : { opacity: 0, y: 6 }}
      animate={reduced ? undefined : { opacity: 1, y: 0 }}
      transition={{ duration: 0.28, ease: [0.16, 1, 0.3, 1] }}
    >
      <ConfigHero
        version={config.data?.version}
        sectionCount={filterOptions.length - 1}
        pendingCount={pendingCount}
        offline={offline}
        saveDisabled={!initialized || saveMutation.isPending}
        validateDisabled={!initialized || validateMutation.isPending}
        saving={saveMutation.isPending}
        validating={validateMutation.isPending}
        successId={successId}
        onSave={() => saveMutation.mutate()}
        onValidate={() => validateMutation.mutate()}
        onOpenPalette={() => palette.setOpen(true)}
      />

      <motion.section
        className="grid grid-cols-1 gap-3.5 md:grid-cols-2 xl:grid-cols-4"
        variants={variants.stagger}
        initial="hidden"
        animate="visible"
      >
        <StatChip
          variant="primary"
          live={!offline}
          label={t("config.tp.statSections")}
          value={offline ? "—" : filterOptions.length - 1}
          foot={t("config.tp.statSectionsFoot")}
          sparkPath={SPARK_SECTIONS}
          sparkTone="amber"
        />
        <StatChip
          label={t("config.tp.statPending")}
          value={offline ? "—" : pendingCount}
          delta={
            !offline && isDirty
              ? { label: t("config.sectionModified", { defaultValue: "dirty" }), tone: "down" }
              : !offline
                ? { label: t("common.saved", { defaultValue: "clean" }), tone: "up" }
                : undefined
          }
          foot={
            offline
              ? t("config.tp.offlineBlockHint")
              : isDirty
                ? t("config.tp.statPendingFoot", { n: pendingCount })
                : t("config.tp.statPendingClean")
          }
          sparkPath={SPARK_PENDING}
          sparkTone={isDirty ? "ember" : "peach"}
        />
        <StatChip
          label={t("config.tp.statValidators")}
          value={latestResult ? latestResult.issues.length : "—"}
          foot={t("config.tp.statValidatorsFoot")}
          sparkPath={SPARK_VALIDATORS}
          sparkTone="peach"
        />
        <StatChip
          label={t("config.tp.statLastSave")}
          value={lastSavedLabel}
          foot={
            lastSavedAt === null
              ? t("config.tp.statLastSaveNever")
              : config.data?.version
                ? t("config.tp.lastSavedVersion", { v: config.data.version })
                : ""
          }
          sparkPath={SPARK_LAST_SAVE}
          sparkTone="ember"
        />
      </motion.section>

      <FilterChipGroup
        label={t("config.sections")}
        options={filterOptions}
        value={activeSection}
        onChange={(next) => jumpToSection(next as SectionId | "all")}
      />

      {/* Active section description strip. "all" omits this so the editor
          gets the full vertical space. */}
      {activeSection !== "all" ? (
        <GlassPanel variant="soft" className="flex flex-wrap items-center justify-between gap-3 px-4 py-3">
          <div className="flex min-w-0 flex-col gap-1">
            <div className="flex items-center gap-2 font-mono text-[12px] text-tp-amber">
              <code>[{sectionLabels[activeSection] ?? activeSection}]</code>
              {dirty.has(activeSection) ? (
                <span className="rounded-full border border-tp-amber/30 bg-tp-amber-soft px-1.5 py-[1px] font-mono text-[9.5px] font-medium uppercase tracking-wide text-tp-amber">
                  {t("config.tp.sectionModified")}
                </span>
              ) : null}
            </div>
            <p className="text-[12.5px] text-tp-ink-2">
              {sectionDescriptions[activeSection] ?? t("config.tp.sectionEmpty")}
            </p>
          </div>
          <p className="text-[11px] text-tp-ink-4">
            {t("config.tp.sectionHint", { name: activeSection })}
          </p>
        </GlassPanel>
      ) : null}

      <section className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_380px]">
        <GlassPanel variant="soft" className="flex min-h-[600px] flex-col overflow-hidden p-0">
          <div className="flex items-center justify-between border-b border-tp-glass-edge px-4 py-2.5">
            <div className="flex items-center gap-2 font-mono text-[11px] text-tp-ink-3">
              <FileCode2 className="h-3.5 w-3.5 text-tp-amber" aria-hidden />
              {t("config.tp.editorLabel")}
              {config.data?.version ? (
                <span className="rounded-md border border-tp-glass-edge bg-tp-glass-inner px-1.5 py-[1px] font-mono text-[10.5px] text-tp-ink-3">
                  {t("config.version", { v: config.data.version })}
                </span>
              ) : null}
            </div>
            {isDirty ? (
              <span className="rounded-full border border-tp-amber/30 bg-tp-amber-soft px-2 py-[2px] font-mono text-[10px] font-medium uppercase tracking-wide text-tp-amber">
                {pendingCount === 1
                  ? t("config.tp.pendingBarLeadSingular")
                  : t("config.tp.pendingBarLead", { n: pendingCount })}
              </span>
            ) : null}
          </div>
          <div className="flex-1">
            {config.isPending ? (
              <div className="h-[600px] animate-pulse bg-tp-glass-inner" aria-hidden />
            ) : offline ? (
              <OfflineBlock message={(config.error as Error | undefined)?.message} />
            ) : (
              <Editor
                height="600px"
                defaultLanguage="ini"
                value={draft}
                onChange={(v) => setDraft(v ?? "")}
                theme={resolvedTheme === "light" ? "vs-light" : "vs-dark"}
                onMount={onMount}
                options={{
                  fontSize: 13,
                  minimap: { enabled: false },
                  wordWrap: "on",
                  scrollBeyondLastLine: false,
                  fontFamily: "var(--font-geist-mono), ui-monospace, monospace",
                }}
              />
            )}
          </div>
        </GlassPanel>

        {/* Right rail — validation drawer. Renders as a lightweight placeholder
            when no result is present so the grid stays aligned. */}
        <div className="lg:sticky lg:top-4 lg:self-start">
          {drawerOpen && latestResult ? (
            <ValidationDrawer
              result={latestResult}
              onClose={() => setDrawerOpen(false)}
            />
          ) : (
            <IdleDrawer
              hasResult={!!latestResult}
              onOpen={() => latestResult && setDrawerOpen(true)}
            />
          )}
        </div>
      </section>

      {/* Error banners — wrapped as GlassPanel so they inherit the glass glow */}
      {validateMutation.isError ? (
        <GlassPanel
          variant="soft"
          className="border-tp-err/30 bg-tp-err-soft px-4 py-2 text-[12.5px] text-tp-err"
          role="alert"
        >
          {t("config.validateFailed")}: {(validateMutation.error as Error).message}
        </GlassPanel>
      ) : null}
      {saveMutation.isError ? (
        <GlassPanel
          variant="soft"
          className="border-tp-err/30 bg-tp-err-soft px-4 py-2 text-[12.5px] text-tp-err"
          role="alert"
        >
          {t("common.saveFailed")}: {(saveMutation.error as Error).message}
        </GlassPanel>
      ) : null}

      {/* E2E lifeline — a headless paragraph whose contents match
          `new version: <sha>` after a successful save. The Playwright spec
          asserts on this exact substring; keeping it as a tiny, off-canvas
          line means we don't have to retool the test alongside the reskin. */}
      {saveResult?.version ? (
        <p className="sr-only" aria-live="polite">
          {t("config.newVersion", { v: saveResult.version })}
        </p>
      ) : null}

      {/* Sticky bottom save bar — only when dirty. */}
      <PendingBar
        pendingCount={pendingCount}
        visible={isDirty && initialized}
        saving={saveMutation.isPending}
        onSave={() => saveMutation.mutate()}
        onDiscard={() => config.data && setDraft(config.data.toml)}
      />
    </motion.div>
  );
}

// ---- hero ----------------------------------------------------------------

interface ConfigHeroProps {
  version: string | undefined;
  sectionCount: number;
  pendingCount: number;
  offline: boolean;
  saveDisabled: boolean;
  validateDisabled: boolean;
  saving: boolean;
  validating: boolean;
  successId: number;
  onSave: () => void;
  onValidate: () => void;
  onOpenPalette: () => void;
}

function ConfigHero({
  version,
  sectionCount,
  pendingCount,
  offline,
  saveDisabled,
  validateDisabled,
  saving,
  validating,
  successId,
  onSave,
  onValidate,
  onOpenPalette,
}: ConfigHeroProps) {
  const { t } = useTranslation();
  const isDirty = pendingCount > 0;

  return (
    <GlassPanel
      variant="strong"
      as="section"
      className="relative overflow-hidden p-7"
    >
      {/* Ambient amber/ember glow layers — match scheduler / approvals hero. */}
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
        <div className="inline-flex w-fit items-center gap-2.5 rounded-full border border-tp-glass-edge bg-tp-glass-inner-strong py-1 pl-2 pr-3 font-mono text-[11px] text-tp-ink-2">
          <span
            className={cn(
              "h-1.5 w-1.5 rounded-full",
              offline
                ? "bg-tp-err"
                : isDirty
                  ? "bg-tp-amber tp-breathe-amber"
                  : "bg-tp-ok",
            )}
          />
          {version ? t("config.tp.lastSavedVersion", { v: version }) : "corlinman.toml"}
        </div>

        <h1 className="text-balance font-sans text-[28px] font-semibold leading-[1.15] tracking-[-0.025em] text-tp-ink sm:text-[32px]">
          {t("config.tp.heroTitle")}
        </h1>

        <p className="max-w-[72ch] text-[14.5px] leading-[1.6] text-tp-ink-2">
          {offline
            ? t("config.tp.heroLeadOffline")
            : isDirty
              ? t("config.tp.heroLeadDirty", { n: sectionCount, dirty: pendingCount })
              : t("config.tp.heroLeadClean", { n: sectionCount })}
        </p>

        <div className="mt-1 flex flex-wrap items-center gap-2.5">
          {/* Primary Save CTA with amber ripple on success. `position:relative`
              is critical — <SuccessRipple> is absolutely positioned inside
              this span. */}
          <span className="relative inline-flex overflow-visible">
            <button
              type="button"
              onClick={onSave}
              disabled={saveDisabled}
              data-testid="config-save-btn"
              className={cn(
                "relative inline-flex items-center gap-2 rounded-lg border border-tp-amber/40 bg-tp-amber px-3.5 py-2 text-[13px] font-medium text-tp-glass-hl shadow-[0_10px_30px_-12px_var(--tp-amber-glow)]",
                "transition-all hover:brightness-[1.04] hover:-translate-y-[0.5px]",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/60",
                "disabled:cursor-not-allowed disabled:opacity-70 disabled:hover:translate-y-0",
                saving && "animate-pulse",
              )}
            >
              {saving ? t("config.saving") : t("config.save")}
            </button>
            <SuccessRipple id={successId} />
          </span>

          <button
            type="button"
            onClick={onValidate}
            disabled={validateDisabled}
            data-testid="config-validate-btn"
            className={cn(
              "inline-flex items-center gap-2 rounded-lg border border-tp-glass-edge bg-tp-glass-inner px-3 py-2 text-[13px] font-medium text-tp-ink-2",
              "transition-colors hover:bg-tp-glass-inner-hover hover:text-tp-ink",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/40",
              "disabled:cursor-not-allowed disabled:opacity-70",
            )}
          >
            {validating ? t("config.validating") : t("config.tp.ctaValidate")}
            <span className="ml-1 rounded bg-black/5 px-1.5 py-0.5 font-mono text-[10px] text-tp-ink-3 dark:bg-white/5">
              {t("config.tp.shortcutValidate")}
            </span>
          </button>

          <button
            type="button"
            onClick={onOpenPalette}
            className="inline-flex items-center gap-2 rounded-lg border border-tp-glass-edge bg-tp-glass-inner px-3 py-2 text-[13px] font-medium text-tp-ink-2 transition-colors hover:bg-tp-glass-inner-hover hover:text-tp-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/40"
          >
            <Search className="h-3.5 w-3.5" aria-hidden />
            {t("config.tp.ctaPaletteHint")}
            <span className="ml-1 rounded bg-black/5 px-1.5 py-0.5 font-mono text-[10px] text-tp-ink-3 dark:bg-white/5">
              ⌘K
            </span>
          </button>
        </div>
      </div>
    </GlassPanel>
  );
}

// ---- validation drawer ---------------------------------------------------

function ValidationDrawer({
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
          <p className="text-[13px] text-tp-ink-2">
            {t("config.tp.validationOkHint")}
          </p>
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

function IdleDrawer({
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

// ---- offline block -------------------------------------------------------

function OfflineBlock({ message }: { message?: string }) {
  const { t } = useTranslation();
  const firstLine = message?.split(/\r?\n/).find((ln) => ln.trim().length > 0)?.trim();
  const isHtmlDump = firstLine?.startsWith("<");
  const short = isHtmlDump
    ? undefined
    : firstLine && firstLine.length > 180
      ? firstLine.slice(0, 180) + "…"
      : firstLine;
  return (
    <div className="flex h-full flex-col items-center justify-center gap-2 p-8 text-center">
      <div className="font-mono text-[11px] uppercase tracking-[0.12em] text-tp-err">
        {t("config.tp.offlineBlockTitle")}
      </div>
      <p className="max-w-prose text-[13px] text-tp-ink-2">
        {t("config.tp.offlineBlockHint")}
      </p>
      {short ? (
        <p className="max-w-full truncate font-mono text-[11px] text-tp-ink-4" title={message}>
          {short}
        </p>
      ) : null}
    </div>
  );
}

// ---- sticky pending bar --------------------------------------------------

function PendingBar({
  pendingCount,
  visible,
  saving,
  onSave,
  onDiscard,
}: {
  pendingCount: number;
  visible: boolean;
  saving: boolean;
  onSave: () => void;
  onDiscard: () => void;
}) {
  const { t } = useTranslation();
  if (!visible) return null;
  return (
    <div
      className="pointer-events-none fixed inset-x-0 bottom-4 z-30 flex justify-center px-4"
      role="region"
      aria-label={t("config.tp.pendingBarLead", { n: pendingCount })}
    >
      <GlassPanel
        variant="strong"
        className="pointer-events-auto flex w-full max-w-[720px] items-center gap-3 px-4 py-3"
      >
        <span className="inline-flex h-2 w-2 rounded-full bg-tp-amber tp-breathe-amber" aria-hidden />
        <span className="flex-1 text-[13px] text-tp-ink">
          {pendingCount === 1
            ? t("config.tp.pendingBarLeadSingular")
            : t("config.tp.pendingBarLead", { n: pendingCount })}
        </span>
        <button
          type="button"
          onClick={onDiscard}
          disabled={saving}
          className={cn(
            "inline-flex items-center gap-1.5 rounded-lg border border-tp-glass-edge bg-tp-glass-inner px-3 py-1.5 text-[12px] font-medium text-tp-ink-2",
            "transition-colors hover:bg-tp-glass-inner-hover hover:text-tp-ink",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/40",
            "disabled:cursor-not-allowed disabled:opacity-60",
          )}
        >
          <RotateCcw className="h-3.5 w-3.5" aria-hidden />
          {t("config.tp.discardChanges")}
        </button>
        <button
          type="button"
          onClick={onSave}
          disabled={saving}
          className={cn(
            "inline-flex items-center gap-1.5 rounded-lg border border-tp-amber/40 bg-tp-amber px-3.5 py-1.5 text-[12px] font-medium text-tp-glass-hl",
            "transition-all hover:brightness-[1.04]",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/60",
            "disabled:cursor-not-allowed disabled:opacity-70",
          )}
        >
          {saving
            ? t("config.saving")
            : pendingCount === 1
              ? t("config.save")
              : `${t("config.save")} · ${pendingCount}`}
        </button>
      </GlassPanel>
    </div>
  );
}
