"use client";

import { Menu } from "lucide-react";

import { Breadcrumbs } from "./breadcrumbs";
import { HealthDot } from "./health-dot";
import { LanguageToggle } from "./language-toggle";
import { SearchTrigger } from "./search-trigger";
import { useMobileDrawer } from "./mobile-drawer-context";
import { ThemeToggle } from "@/components/ui/theme-toggle";
import { cn } from "@/lib/utils";

/**
 * Tidepool topbar. Floating glass panel — matches sidebar's 16px gutter
 * treatment. Left: breadcrumbs. Right: search (⌘K), health dot, language,
 * theme. Logout + user info live in the sidebar.
 *
 * Mobile (<md): leading slot carries a hamburger that opens the sidebar
 * drawer (the sidebar itself is hidden off-canvas until then).
 */
export function TopNav() {
  const { toggle, open } = useMobileDrawer();
  return (
    <header
      className={cn(
        "sticky top-2 md:top-4 z-40 flex h-14 items-center justify-between gap-2 md:gap-4 rounded-2xl border border-tp-glass-edge bg-tp-glass px-3 md:px-4",
        "shadow-[inset_0_1px_0_var(--tp-glass-hl)] shadow-tp-panel backdrop-blur-glass backdrop-saturate-glass",
      )}
    >
      <div className="flex min-w-0 items-center gap-2 md:gap-3">
        <button
          type="button"
          onClick={toggle}
          aria-label="Toggle navigation drawer"
          aria-expanded={open}
          aria-controls="admin-sidebar"
          data-testid="mobile-nav-trigger"
          className="-ml-1 inline-flex h-8 w-8 items-center justify-center rounded-md text-tp-ink-2 transition-colors hover:bg-tp-glass-inner hover:text-tp-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/40 md:hidden"
        >
          <Menu className="h-5 w-5" aria-hidden />
        </button>
        <Breadcrumbs />
      </div>
      <div className="flex items-center gap-1.5 md:gap-2">
        <SearchTrigger />
        <div className="hidden h-5 w-px bg-tp-glass-edge md:block" />
        <HealthDot className="hidden md:inline-flex" />
        <LanguageToggle />
        <ThemeToggle />
      </div>
    </header>
  );
}
