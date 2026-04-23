"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import { Search } from "lucide-react";

import { cn } from "@/lib/utils";
import { GlassPanel } from "@/components/ui/glass-panel";
import { useCommandPalette } from "@/components/cmdk-palette";
import { SuccessRipple } from "./success-ripple";

/**
 * `<ConfigHero>` — glass-strong header strip for the Config page.
 *
 * Mirrors the Scheduler / Plugins hero rhythm: lead pill with live
 * version, prose summary (dirty vs clean vs offline), and the three
 * primary CTAs (Save, Validate, ⌘K palette).
 *
 * The Save button carries the E2E `config-save-btn` testid. Its textContent
 * is exactly `config.save` at rest and `config.saving` in-flight so the
 * vitest unit test can assert on `textContent` directly.
 *
 * The `<SuccessRipple>` is absolutely positioned inside the button's parent
 * `<span>`; `overflow-visible` on the wrapper is load-bearing for the
 * animation to escape the button box.
 */
export interface ConfigHeroProps {
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
}

export function ConfigHero({
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
}: ConfigHeroProps) {
  const { t } = useTranslation();
  const palette = useCommandPalette();
  const isDirty = pendingCount > 0;

  return (
    <GlassPanel
      variant="strong"
      as="section"
      className="relative overflow-hidden p-7"
    >
      {/* Ambient amber + ember glow — same radial-gradient language used
          on the Scheduler/Plugins hero. */}
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
            onClick={() => palette.setOpen(true)}
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

export default ConfigHero;
