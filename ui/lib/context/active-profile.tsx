"use client";

/**
 * Active-profile context (W3.4 — front-end profile switcher).
 *
 * Persists the operator's currently selected profile *slug* in
 * ``localStorage`` under :data:`STORAGE_KEY` and exposes it via
 * :func:`useActiveProfile` so feature pages (``/chat``, ``/skills``, …)
 * can scope their backend calls to the active profile without each
 * route re-reading the storage key.
 *
 * The provider:
 *
 *   1. Hydrates ``slug`` from ``localStorage`` on mount (falls back to
 *      ``"default"`` when nothing is stored or when running on the
 *      server / in tests without ``localStorage``).
 *   2. Fetches the full list of profiles via ``listProfiles()`` so we
 *      can resolve the slug → :class:`Profile` object. We don't 404-flap
 *      if the stored slug points at a deleted profile — instead we drop
 *      back to ``"default"`` and rewrite storage so the next reload is
 *      clean.
 *   3. Listens for ``storage`` events from other tabs so a switch in
 *      tab A reflects in tab B without a manual reload.
 *
 * The provider does *not* call the backend on switch — the slug is a
 * client-side scope value only. Chat / skills routes will pass it as a
 * query string param when they consume it.
 */

import * as React from "react";
import { useQuery } from "@tanstack/react-query";

import { listProfiles, type Profile } from "@/lib/api";

export const STORAGE_KEY = "corlinman_active_profile";
export const DEFAULT_SLUG = "default";

interface ActiveProfileContextValue {
  /** The currently active profile slug. */
  slug: string;
  /** Setter — writes to localStorage and broadcasts to other tabs. */
  setSlug: (next: string) => void;
  /** Resolved profile row from /admin/profiles, or ``null`` while loading. */
  profile: Profile | null;
  /** Every known profile — useful for dropdown rendering. */
  profiles: Profile[];
  /** True while the initial /admin/profiles call is in-flight. */
  loading: boolean;
}

const ActiveProfileContext =
  React.createContext<ActiveProfileContextValue | null>(null);

function readStoredSlug(): string {
  if (typeof window === "undefined") return DEFAULT_SLUG;
  try {
    const v = window.localStorage.getItem(STORAGE_KEY);
    return v && v.trim() ? v : DEFAULT_SLUG;
  } catch {
    return DEFAULT_SLUG;
  }
}

function writeStoredSlug(slug: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, slug);
  } catch {
    /* SSR / private mode — silently ignore. */
  }
}

export interface ActiveProfileProviderProps {
  children: React.ReactNode;
}

export function ActiveProfileProvider({
  children,
}: ActiveProfileProviderProps): React.ReactElement {
  // ``hydrated`` gate avoids the SSR/CSR mismatch warning — initial render
  // always returns ``"default"`` so the server-rendered tree matches; the
  // real localStorage value is wired on the first effect tick.
  const [slug, setSlugState] = React.useState<string>(DEFAULT_SLUG);
  const [hydrated, setHydrated] = React.useState(false);

  React.useEffect(() => {
    setSlugState(readStoredSlug());
    setHydrated(true);
  }, []);

  // Cross-tab sync: react to localStorage events fired by *other* tabs.
  // (Same-tab updates don't fire ``storage`` events — we handle those
  // via the explicit ``setSlug`` setter.)
  React.useEffect(() => {
    function onStorage(e: StorageEvent) {
      if (e.key !== STORAGE_KEY) return;
      const next = (e.newValue ?? DEFAULT_SLUG).trim() || DEFAULT_SLUG;
      setSlugState(next);
    }
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  const profilesQuery = useQuery({
    queryKey: ["admin", "profiles"],
    queryFn: listProfiles,
    // Profile lists rarely change — keep aggressive cache.
    staleTime: 30_000,
    retry: false,
  });

  const profiles = profilesQuery.data?.profiles ?? [];
  const loading = profilesQuery.isPending;
  const fetching = profilesQuery.isFetching;
  // ``dataUpdatedAt`` ticks when the query resolves; we only re-evaluate
  // the self-heal predicate on a fresh resolution so a manual
  // ``setSlug`` immediately followed by an ``invalidateQueries`` doesn't
  // clobber the operator's selection mid-flight.
  const dataUpdatedAt = profilesQuery.dataUpdatedAt;

  // Self-heal: if the freshly-resolved profile list lacks the active
  // slug, snap back to ``default``. Intentionally NOT keyed on ``slug``
  // — a setSlug() call inside the same tick as the refetch trigger
  // would otherwise race the cache (old list says the slug is missing
  // → reset → user's selection vanishes).
  React.useEffect(() => {
    if (!hydrated || loading || fetching) return;
    if (profiles.length === 0) return;
    const current = readStoredSlug();
    const exists = profiles.some((p) => p.slug === current);
    if (!exists && current !== DEFAULT_SLUG) {
      writeStoredSlug(DEFAULT_SLUG);
      setSlugState(DEFAULT_SLUG);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hydrated, dataUpdatedAt]);

  const setSlug = React.useCallback((next: string) => {
    const trimmed = next.trim() || DEFAULT_SLUG;
    writeStoredSlug(trimmed);
    setSlugState(trimmed);
  }, []);

  const profile =
    profiles.find((p) => p.slug === slug) ?? null;

  const value: ActiveProfileContextValue = React.useMemo(
    () => ({ slug, setSlug, profile, profiles, loading }),
    [slug, setSlug, profile, profiles, loading],
  );

  return (
    <ActiveProfileContext.Provider value={value}>
      {children}
    </ActiveProfileContext.Provider>
  );
}

/** Hook — must be called inside an :class:`ActiveProfileProvider`. */
export function useActiveProfile(): ActiveProfileContextValue {
  const ctx = React.useContext(ActiveProfileContext);
  if (!ctx) {
    throw new Error(
      "useActiveProfile() must be called inside <ActiveProfileProvider />",
    );
  }
  return ctx;
}
