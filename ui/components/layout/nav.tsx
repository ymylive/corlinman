"use client";

import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

/**
 * Top bar. Shows product name, build channel badge, and (later) a user menu.
 *
 * TODO(M6): wire the session dropdown + theme toggle + locale switcher.
 */
export function TopNav() {
  return (
    <header className="flex h-14 items-center justify-between border-b border-border bg-background/95 px-6 backdrop-blur supports-[backdrop-filter]:bg-background/60">
      <div className="flex items-center gap-3">
        <Link href="/" className="font-semibold tracking-tight">
          corlinman
        </Link>
        <Badge variant="outline" className="text-xs">
          0.1.0 · M0
        </Badge>
      </div>
      <div className="flex items-center gap-2">
        <Button variant="ghost" size="sm" aria-label="theme" disabled>
          {/* TODO(M6): theme toggle via next-themes */}
          Theme
        </Button>
        <Button variant="ghost" size="sm" aria-label="locale" disabled>
          {/* TODO(M6): locale switcher (zh / en) */}
          中/EN
        </Button>
      </div>
    </header>
  );
}
