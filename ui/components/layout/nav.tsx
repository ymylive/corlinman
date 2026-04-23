"use client";

import { Breadcrumbs } from "./breadcrumbs";
import { HealthDot } from "./health-dot";
import { LanguageToggle } from "./language-toggle";
import { SearchTrigger } from "./search-trigger";
import { ThemeToggle } from "@/components/ui/theme-toggle";

/**
 * Tidepool topbar. Floating glass panel — matches sidebar's 16px gutter
 * treatment. Left: breadcrumbs. Right: search (⌘K), health dot, language,
 * theme. Logout + user info live in the sidebar.
 *
 * The legacy ThemeToggle (components/layout/theme-toggle.tsx, next-themes
 * based) is replaced by the new Tidepool one (components/ui/theme-toggle.tsx)
 * which sets both `data-theme` attribute AND the `.dark` class, keeping
 * Tailwind's dark: variant working for not-yet-retokened pages.
 */
export function TopNav() {
  return (
    <header
      className="sticky top-4 z-40 flex h-14 items-center justify-between gap-4 rounded-2xl border border-tp-glass-edge bg-tp-glass px-4 shadow-[inset_0_1px_0_var(--tp-glass-hl)] shadow-tp-panel backdrop-blur-glass backdrop-saturate-glass"
    >
      <div className="flex items-center gap-3 overflow-hidden">
        <Breadcrumbs />
      </div>
      <div className="flex items-center gap-2">
        <SearchTrigger />
        <div className="hidden h-5 w-px bg-tp-glass-edge md:block" />
        <HealthDot className="hidden md:inline-flex" />
        <LanguageToggle />
        <ThemeToggle />
      </div>
    </header>
  );
}
