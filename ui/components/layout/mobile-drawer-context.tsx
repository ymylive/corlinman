"use client";

import * as React from "react";

/**
 * Shared open/close state for the mobile sidebar drawer. Lives in the
 * admin layout root so both the <TopNav> hamburger and the <Sidebar>
 * slide-in can coordinate without prop-drilling through every page.
 *
 * On desktop (≥md) the drawer state is irrelevant — the sidebar is a
 * permanent flex column and reads `open` as a no-op.
 */

interface DrawerState {
  open: boolean;
  setOpen: (next: boolean) => void;
  toggle: () => void;
}

const Ctx = React.createContext<DrawerState | null>(null);

export function MobileDrawerProvider({
  children,
}: {
  children: React.ReactNode;
}) {
  const [open, setOpen] = React.useState(false);
  const value = React.useMemo<DrawerState>(
    () => ({
      open,
      setOpen,
      toggle: () => setOpen((v) => !v),
    }),
    [open],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

/**
 * Fallback shape when the hook is called outside the provider — e.g. in a
 * unit test rendering <Sidebar> in isolation. The drawer is "closed" and
 * setters are no-ops so the component still renders sensibly on desktop.
 */
const NOOP: DrawerState = {
  open: false,
  setOpen: () => {},
  toggle: () => {},
};

export function useMobileDrawer(): DrawerState {
  return React.useContext(Ctx) ?? NOOP;
}
