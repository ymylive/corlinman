import type { Runner } from "@/lib/mocks/nodes";

/**
 * Capability taxonomy for the Nodes page.
 *
 * The runner's advertised tool names follow a `namespace.operation` convention
 * (e.g. `file_ops.read`, `web_search.query`). We treat the namespace prefix as
 * the capability domain — the page exposes it as a filter chip. This is a
 * thin convention over the mock data; the real `/wstool/runners` response
 * shipped by B4-BE3 is expected to surface capabilities as a first-class
 * field, at which point this helper becomes a trivial lookup.
 */

export const CAPABILITY_LABELS: Record<string, string> = {
  web_search: "web_search",
  browser: "browser",
  file_ops: "file_ops",
  canvas: "canvas",
  gh_issues: "gh_issues",
  memory: "memory",
  gemini: "gemini",
  discord: "discord",
  bear_notes: "bear_notes",
};

/** Returns the de-duplicated list of capability namespaces for a runner. */
export function capabilityOf(runner: Runner): string[] {
  const seen = new Set<string>();
  for (const t of runner.tools) {
    const dot = t.indexOf(".");
    const ns = dot === -1 ? t : t.slice(0, dot);
    seen.add(ns);
  }
  return Array.from(seen);
}

/** Counts runners per capability across the whole list (for chip badges). */
export function capabilityCounts(runners: Runner[]): Map<string, number> {
  const out = new Map<string, number>();
  for (const r of runners) {
    for (const c of capabilityOf(r)) {
      out.set(c, (out.get(c) ?? 0) + 1);
    }
  }
  return out;
}
