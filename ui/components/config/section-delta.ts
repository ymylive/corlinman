/**
 * Section-delta helpers for the Config editor.
 *
 * The TOML draft is a single string buffer; the operator navigates it by
 * top-level section header (`[server]`, `[[scheduler.jobs]]`, …). We compute
 * two derived quantities off that buffer:
 *
 *   1. `findSectionLine(toml, section)` — line index of the first header
 *      that matches `[section]` / `[section.…]` / `[[section.…]]`. Used by
 *      the "jump to section" side-chip to move the Monaco cursor.
 *
 *   2. `dirtySections(original, draft)` — set of section names whose
 *      contents changed between the two strings. Drives the amber
 *      "modified" dot on section chips and the pending-counter pill.
 *
 *   3. `sectionCounts(draft)` — line count per section, shown as the small
 *      numeric suffix on each chip.
 *
 * All three walk the raw string once per call; callers should memoise.
 */

/** Return the 1-based line number of the first header matching `section`, or null. */
export function findSectionLine(toml: string, section: string): number | null {
  const lines = toml.split("\n");
  const marker = `[${section}]`;
  const markerTable = `[${section}.`;
  const markerArray = `[[${section}.`;
  for (let i = 0; i < lines.length; i++) {
    const trimmed = lines[i]!.trimStart();
    if (
      trimmed.startsWith(marker) ||
      trimmed.startsWith(markerTable) ||
      trimmed.startsWith(markerArray)
    ) {
      return i + 1;
    }
  }
  return null;
}

/**
 * Bucket source lines by the most recently seen top-level table name.
 * `[section.sub]` and `[[section.list]]` both roll up into `section`.
 */
function bucketBySection(src: string[]): Map<string, string[]> {
  const out = new Map<string, string[]>();
  let section: string | null = null;
  for (const ln of src) {
    const t = ln.trimStart();
    const m = t.match(/^\[\[?([A-Za-z0-9_]+)/);
    if (m) section = m[1]!;
    if (section) {
      const bucket = out.get(section) ?? [];
      bucket.push(ln);
      out.set(section, bucket);
    }
  }
  return out;
}

/** Return the set of top-level section names whose body differs across the two strings. */
export function dirtySections(original: string, draft: string): Set<string> {
  if (original === draft) return new Set();
  const out = new Set<string>();
  const a = bucketBySection(original.split("\n"));
  const b = bucketBySection(draft.split("\n"));
  const keys = new Set<string>([...a.keys(), ...b.keys()]);
  for (const k of keys) {
    const ax = a.get(k)?.join("\n") ?? "";
    const bx = b.get(k)?.join("\n") ?? "";
    if (ax !== bx) out.add(k);
  }
  return out;
}

/** Count the lines attributed to each top-level section. */
export function sectionCounts(draft: string): Map<string, number> {
  const map = new Map<string, number>();
  let current: string | null = null;
  for (const ln of draft.split("\n")) {
    const t = ln.trimStart();
    const m = t.match(/^\[\[?([A-Za-z0-9_]+)/);
    if (m) current = m[1]!;
    if (current) map.set(current, (map.get(current) ?? 0) + 1);
  }
  return map;
}
