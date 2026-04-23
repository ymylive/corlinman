"use client";

import { useTranslation } from "react-i18next";
import {
  FilterChipGroup,
  type FilterChipOption,
} from "@/components/ui/filter-chip-group";
import type { Runner } from "@/lib/mocks/nodes";
import { capabilityCounts } from "./capabilities";

/**
 * Capability filter row for the Nodes page — a sentinel `all` option plus
 * one chip per capability namespace present in the current runner list.
 *
 * The special value `"all"` maps to `capabilityFilter === null` in the parent
 * page state; selecting any other chip sets the filter to that capability
 * string. Child viz + side rail dim-out non-matching nodes.
 */

export const ALL_SENTINEL = "all";

export interface CapabilityFilterProps {
  runners: Runner[];
  /** `null` means "all". */
  value: string | null;
  onChange: (next: string | null) => void;
}

export function CapabilityFilter({
  runners,
  value,
  onChange,
}: CapabilityFilterProps) {
  const { t } = useTranslation();
  const counts = capabilityCounts(runners);

  // Stable order: alphabetic. Keeps the chip row from shuffling on re-render.
  const capKeys = Array.from(counts.keys()).sort();

  const options: FilterChipOption[] = [
    {
      value: ALL_SENTINEL,
      label: t("nodes.tp.filterAll"),
      count: runners.length,
      tone: "neutral" as const,
    },
    ...capKeys.map((cap) => ({
      value: cap,
      label: cap,
      count: counts.get(cap) ?? 0,
      tone: "neutral" as const,
    })),
  ];

  return (
    <FilterChipGroup
      label={t("nodes.tp.filterAria")}
      options={options}
      value={value ?? ALL_SENTINEL}
      onChange={(next) => onChange(next === ALL_SENTINEL ? null : next)}
    />
  );
}

export default CapabilityFilter;
