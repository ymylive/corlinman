"use client";

import { Breadcrumbs } from "./breadcrumbs";
import { HealthDot } from "./health-dot";
import { LanguageToggle } from "./language-toggle";
import { SearchTrigger } from "./search-trigger";
import { ThemeToggle } from "./theme-toggle";

/**
 * 56px topbar. Left: breadcrumbs. Right: search (⌘K), health dot,
 * language, theme. Logout + user info live in the sidebar.
 */
export function TopNav() {
  return (
    <header className="sticky top-0 z-40 flex h-14 items-center justify-between gap-4 border-b border-border bg-background/80 px-4 backdrop-blur supports-[backdrop-filter]:bg-background/60">
      <div className="flex items-center gap-3 overflow-hidden">
        <Breadcrumbs />
      </div>
      <div className="flex items-center gap-2">
        <SearchTrigger />
        <div className="hidden h-5 w-px bg-border md:block" />
        <HealthDot className="hidden md:inline-flex" />
        <LanguageToggle />
        <ThemeToggle />
      </div>
    </header>
  );
}
