"use client";

/**
 * <MustChangePasswordProvider /> — shared "is the admin still on the
 * default seed?" flag for the admin shell.
 *
 * Why a context: the banner (top of layout) and the guard (wraps the
 * page body) need to agree on the answer without each issuing its own
 * `GET /admin/me`. We piggyback on the AdminLayout's existing
 * `getSession()` call by accepting the session as a prop here.
 *
 * The provider re-evaluates whenever `session` changes — that's how the
 * `/account/security` page tells the rest of the shell the flag flipped
 * after a successful rotation (it refetches and the layout re-renders).
 *
 * If callers want to set the flag eagerly (e.g. the login page does a
 * round-trip and wants to bypass the guard once), they can drop
 * `must_change_password=false` into the session object they pass in.
 */

import * as React from "react";

import type { AdminSession } from "@/lib/auth";

interface MustChangePasswordContextValue {
  /** Snapshot of the flag for the current session. `false` if unknown. */
  mustChange: boolean;
  /** Imperatively flip the flag — used after a successful rotation. */
  setMustChange: (next: boolean) => void;
}

const MustChangePasswordContext =
  React.createContext<MustChangePasswordContextValue | null>(null);

export function MustChangePasswordProvider({
  session,
  children,
}: {
  session: AdminSession | null;
  children: React.ReactNode;
}) {
  // Seed from the prop so SSR + first render reflect the server's view.
  // After mount we keep the value in local state because mutations
  // (rotate password) update the flag before the layout's getSession
  // refetches.
  const [mustChange, setMustChange] = React.useState<boolean>(
    session?.must_change_password === true,
  );

  // Re-sync whenever the parent passes a new session row. We DON'T want
  // to overwrite a user-driven `false` (rotate succeeded → false → guard
  // releases) with a stale `true`, so we only push forward.
  React.useEffect(() => {
    const next = session?.must_change_password === true;
    setMustChange((prev) => (prev && !next ? prev : next));
  }, [session]);

  const value = React.useMemo<MustChangePasswordContextValue>(
    () => ({ mustChange, setMustChange }),
    [mustChange],
  );

  return (
    <MustChangePasswordContext.Provider value={value}>
      {children}
    </MustChangePasswordContext.Provider>
  );
}

export function useMustChangePassword(): MustChangePasswordContextValue {
  const ctx = React.useContext(MustChangePasswordContext);
  if (!ctx) {
    // Outside the provider — safe fallback for unit tests / Storybook
    // renders that aren't worth wrapping. The banner + guard treat
    // `false` as a no-op, so we don't bounce the operator on a stub.
    return { mustChange: false, setMustChange: () => {} };
  }
  return ctx;
}
