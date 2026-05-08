import type { EvolutionProposal, EvolutionRisk } from "@/lib/api";

export type Tab = "pending" | "approved" | "history" | "meta";

export type { EvolutionProposal, EvolutionRisk };

/**
 * The four `EvolutionKind` values that `EvolutionKind::is_meta()` flags
 * on the Rust side (see `rust/crates/corlinman-evolution/src/types.rs`).
 * The Meta tab in `/admin/evolution` filters proposals by membership in
 * this set so operators never confuse a "self-improvement" proposal with
 * a regular memory_op / agent_prompt rewrite.
 */
export const META_KINDS = [
  "engine_config",
  "engine_prompt",
  "observer_filter",
  "cluster_threshold",
] as const;

export type MetaKind = (typeof META_KINDS)[number];

export function isMetaKind(kind: string): kind is MetaKind {
  return (META_KINDS as readonly string[]).includes(kind);
}
