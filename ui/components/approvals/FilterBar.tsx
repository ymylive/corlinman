"use client";

import * as React from "react";
import { Search } from "lucide-react";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

/** Filter controls for the approvals table.
 *
 * - Search: fuzzy match against `plugin.tool` — plain `includes()` on
 *   lowercase, case-insensitive. Good enough for < ~500 rows; if we ever
 *   hit virtual-scroll territory this would want to move into useMemo +
 *   a smarter ranker.
 * - Plugin dropdown: native `<select>` (per approved scope — no Radix
 *   Select this round). Options derived from currently-loaded rows.
 */
export interface FilterBarProps {
  search: string;
  onSearchChange: (next: string) => void;
  pluginFilter: string;
  onPluginFilterChange: (next: string) => void;
  pluginOptions: string[];
}

export function FilterBar({
  search,
  onSearchChange,
  pluginFilter,
  onPluginFilterChange,
  pluginOptions,
}: FilterBarProps) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      <div className="relative flex-1 min-w-[12rem]">
        <Search
          className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground"
          aria-hidden
        />
        <Input
          type="search"
          value={search}
          onChange={(e) => onSearchChange(e.target.value)}
          placeholder="搜 plugin.tool…"
          className="pl-7"
          aria-label="搜索 plugin.tool"
        />
      </div>
      <select
        value={pluginFilter}
        onChange={(e) => onPluginFilterChange(e.target.value)}
        aria-label="按 plugin 过滤"
        className={cn(
          "h-9 rounded-md border border-input bg-transparent px-2 text-sm shadow-sm",
          "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
        )}
      >
        <option value="">全部 plugin</option>
        {pluginOptions.map((p) => (
          <option key={p} value={p}>
            {p}
          </option>
        ))}
      </select>
    </div>
  );
}
