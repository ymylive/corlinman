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
      className="group flex h-8 items-center gap-2 rounded-md border border-border bg-surface px-2.5 text-xs text-muted-foreground transition-colors hover:border-primary/40 hover:bg-accent/60 hover:text-foreground md:w-64 md:justify-between"
    >
      <span className="inline-flex items-center gap-2">
        <Search className="h-3.5 w-3.5" />
        <span className="hidden md:inline">{t("nav.searchPlaceholder")}</span>
      </span>
      <kbd className="hidden rounded border border-border bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground md:inline-flex">
        ⌘K
      </kbd>
    </button>
  );
}
