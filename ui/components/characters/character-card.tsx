"use client";

import * as React from "react";

import { cn } from "@/lib/utils";
import { GlassPanel } from "@/components/ui/glass-panel";
import { useMotion } from "@/components/ui/motion-safe";
import type { AgentCard } from "@/lib/mocks/characters";

/**
 * `<CharacterCard>` — browse-grid cell for a single character (Tidepool).
 *
 * Card is a soft GlassPanel. Header row: a 32px amber→ember gradient avatar
 * showing the first letter of the name (white-on-amber). Body: name,
 * 2-line-clamp description. Footer: tag chips derived from `tools_allowed`.
 *
 * Interaction: clicking the card opens the detail drawer via `onOpen`.
 * `Edit` renders a secondary affordance inside the card so keyboard users
 * with `prefers-reduced-motion` still get a clearly-labelled edit entry
 * without relying on the card-wide click-target semantics.
 *
 * The card-deck flip from the pre-Tidepool phase is retired — the Tidepool
 * browse pattern is hover-lift + click-to-open, consistent with the
 * Plugins and Approvals grids shipped in Phases 5a/5b.
 */

export interface CharacterCardProps {
  card: AgentCard;
  /** Slight random rotate variance in degrees (-1..+1). Passed from parent so
   * the deterministic per-name hash stays stable across rerenders. */
  rotateDeg?: number;
  /** Fired when the card is activated (click, Enter, Space). */
  onOpen: () => void;
  /** Fired when the Edit affordance is activated. Defaults to `onOpen`. */
  onEdit?: () => void;
}

export function CharacterCard({
  card,
  rotateDeg = 0,
  onOpen,
  onEdit,
}: CharacterCardProps) {
  const { reduced } = useMotion();
  const initial = firstLetter(card.name);
  const tags = deriveTags(card);
  const label = `${card.name} — ${card.description}`;

  function onKeyDown(e: React.KeyboardEvent<HTMLDivElement>) {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      onOpen();
    }
  }

  return (
    <div
      style={
        reduced || rotateDeg === 0
          ? undefined
          : { transform: `rotate(${rotateDeg}deg)` }
      }
      data-testid={`character-card-${card.name}`}
      className={cn(
        "group block",
        !reduced &&
          "transition-transform duration-200 ease-tp-ease-out hover:-translate-y-0.5",
      )}
    >
      <GlassPanel
        variant="soft"
        role="button"
        tabIndex={0}
        aria-label={label}
        onClick={onOpen}
        onKeyDown={onKeyDown}
        data-testid={`character-card-back-${card.name}`}
        className={cn(
          "flex h-full cursor-pointer select-none flex-col gap-3 p-4",
          "transition-[box-shadow,border-color] duration-200 ease-tp-ease-out",
          "group-hover:shadow-tp-primary group-focus-visible:shadow-tp-primary",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/50",
        )}
      >
        {/* Row 1 — avatar + name + description */}
        <div className="flex items-start gap-3">
          <Avatar initial={initial} emoji={card.emoji} />
          <div className="min-w-0 flex-1">
            <h3 className="truncate text-[15px] font-medium leading-tight text-tp-ink">
              {card.name}
            </h3>
            <p
              className={cn(
                "mt-1 text-[12.5px] leading-[1.5] text-tp-ink-2",
                "line-clamp-2",
              )}
            >
              {card.description}
            </p>
          </div>
        </div>

        {/* Row 2 — tag chips derived from tools/skills */}
        <ul className="mt-auto flex flex-wrap items-center gap-1.5 pt-1">
          {tags.length === 0 ? (
            <li className="text-[11px] italic text-tp-ink-4">no tags</li>
          ) : (
            tags.map((tag) => (
              <li
                key={tag}
                className={cn(
                  "inline-flex items-center rounded-full border border-tp-glass-edge",
                  "bg-tp-glass-inner px-2 py-[2px] font-mono text-[10px] text-tp-ink-3",
                )}
                data-testid={`character-card-tag-${card.name}-${tag}`}
              >
                {tag}
              </li>
            ))
          )}

          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              (onEdit ?? onOpen)();
            }}
            className={cn(
              "ml-auto inline-flex items-center rounded-md border border-tp-amber/30",
              "bg-tp-amber-soft px-2 py-[3px] text-[11px] font-medium text-tp-amber",
              "transition-colors hover:bg-[color-mix(in_oklch,var(--tp-amber)_22%,transparent)]",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/50",
            )}
            data-testid={`character-card-edit-${card.name}`}
          >
            Edit
          </button>
        </ul>
      </GlassPanel>
    </div>
  );
}

// --- avatar ---------------------------------------------------------------

function Avatar({ initial, emoji }: { initial: string; emoji?: string }) {
  // Emoji trumps initial when available; falls back to the first-letter
  // monogram in a solid amber→ember gradient disc. 32×32.
  return (
    <div
      aria-hidden="true"
      className={cn(
        "flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full",
        "text-sm font-semibold leading-none text-white",
        "shadow-[0_1px_0_rgba(255,255,255,0.28)_inset,0_4px_10px_-6px_rgba(0,0,0,0.35)]",
      )}
      style={{
        background:
          "linear-gradient(135deg, var(--tp-amber) 0%, var(--tp-ember) 100%)",
      }}
    >
      {emoji ? <span className="text-base">{emoji}</span> : <span>{initial}</span>}
    </div>
  );
}

// --- helpers ---------------------------------------------------------------

/** Derived tag set. Today it's `tools_allowed` — the most meaningful signal
 *  for grouping characters — with a lightweight cap so cards don't overflow. */
export function deriveTags(card: AgentCard): string[] {
  const tools = card.tools_allowed ?? [];
  const capped = tools.slice(0, 3);
  if (tools.length > 3) capped.push(`+${tools.length - 3}`);
  return capped;
}

/**
 * First visible letter of a string — honours surrogate pairs for emoji
 * fallbacks and capitalises an ASCII leading letter.
 */
function firstLetter(s: string): string {
  if (!s) return "?";
  const iter = s[Symbol.iterator]();
  const { value } = iter.next();
  const ch = value ?? "?";
  return /[a-zA-Z]/.test(ch) ? ch.toUpperCase() : ch;
}

/**
 * Deterministic hash of a string → a number in [-1, 1]. Used to give each
 * card a stable tilt across rerenders (a naive `Math.random()` would jitter
 * every time React reconciles).
 */
export function tiltForName(name: string): number {
  let h = 0;
  for (let i = 0; i < name.length; i++) {
    h = (h * 31 + name.charCodeAt(i)) | 0;
  }
  // Map to [-1, 1] with two decimals.
  const norm = ((h % 2000) + 2000) % 2000; // [0, 2000)
  return Math.round(norm - 1000) / 1000;
}
