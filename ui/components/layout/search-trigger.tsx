"use client";

import { Search } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useCommandPalette } from "@/components/cmdk-palette";

/** The "Search... ⌘K" pill in the topnav — opens the command palette. */
export function SearchTrigger() {
  const { toggle } = useCommandPalette();
  const { t } = useTranslation();
  return (
    <button
      type="button"
      onClick={toggle}
      aria-label={t("nav.openPalette")}
      className="group flex h-8 items-center gap-2 rounded-lg border border-tp-glass-edge bg-tp-glass-inner px-2.5 text-[12.5px] text-tp-ink-3 transition-colors hover:border-tp-glass-edge-strong hover:bg-tp-glass-inner-hover hover:text-tp-ink-2 md:w-64 md:justify-between"
    >
      <span className="inline-flex items-center gap-2">
        <Search className="h-3.5 w-3.5" />
        <span className="hidden md:inline">{t("nav.searchPlaceholder")}</span>
      </span>
      <kbd className="hidden rounded border border-tp-glass-edge bg-tp-glass-inner-strong px-1.5 py-0.5 font-mono text-[10px] text-tp-ink-3 md:inline-flex">
        ⌘K
      </kbd>
    </button>
  );
}
