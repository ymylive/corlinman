"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";
import type {
  CuratorSkillOrigin,
  CuratorSkillState,
} from "@/lib/api";

/**
 * Tiny presentational badge used in the W4.6 skill list.
 *
 * Two flavours via the `kind` prop:
 *
 *   - `state`  → green / amber / gray for active / stale / archived
 *   - `origin` → blue / purple / pink for bundled / user / agent-created
 *
 * The colour palette is intentionally borrowed from existing Evolution
 * Phase 3 chips (warn-soft / err-soft / info-soft) so the whole page
 * reads consistently. The label is read from the i18n bundle so the
 * surface stays bilingual without per-call literal strings.
 */
export type SkillBadgeKind =
  | { kind: "state"; value: CuratorSkillState }
  | { kind: "origin"; value: CuratorSkillOrigin };

const STATE_CLASSES: Record<CuratorSkillState, string> = {
  active:
    "border-emerald-500/30 bg-emerald-500/15 text-emerald-700 dark:text-emerald-300",
  stale:
    "border-amber-500/30 bg-amber-500/15 text-amber-700 dark:text-amber-300",
  archived:
    "border-zinc-500/30 bg-zinc-500/15 text-zinc-700 dark:text-zinc-300",
};

const ORIGIN_CLASSES: Record<CuratorSkillOrigin, string> = {
  bundled:
    "border-sky-500/30 bg-sky-500/15 text-sky-700 dark:text-sky-300",
  "user-requested":
    "border-violet-500/30 bg-violet-500/15 text-violet-700 dark:text-violet-300",
  "agent-created":
    "border-pink-500/30 bg-pink-500/15 text-pink-700 dark:text-pink-300",
};

const ORIGIN_LABEL_KEYS: Record<CuratorSkillOrigin, string> = {
  bundled: "evolution.skill.origin.bundled",
  "user-requested": "evolution.skill.origin.userRequested",
  "agent-created": "evolution.skill.origin.agentCreated",
};

export function SkillBadge(
  props: SkillBadgeKind & { className?: string },
) {
  const { t } = useTranslation();
  const base =
    "inline-flex items-center rounded-md border px-2 py-0.5 text-[11px] font-medium";

  if (props.kind === "state") {
    return (
      <span
        data-testid={`skill-state-${props.value}`}
        className={cn(base, STATE_CLASSES[props.value], props.className)}
      >
        {t(`evolution.skill.state.${props.value}`)}
      </span>
    );
  }

  return (
    <span
      data-testid={`skill-origin-${props.value}`}
      className={cn(base, ORIGIN_CLASSES[props.value], props.className)}
    >
      {t(ORIGIN_LABEL_KEYS[props.value])}
    </span>
  );
}
