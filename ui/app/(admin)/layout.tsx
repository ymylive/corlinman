"use client";

import { useEffect, useState } from "react";
import { usePathname, useRouter } from "next/navigation";

import { getSession, type AdminSession } from "@/lib/auth";
import { TopNav } from "@/components/layout/nav";
import { Sidebar } from "@/components/layout/sidebar";
import { PageTransition } from "@/components/layout/page-transition";
import { RouteScrollRestore } from "@/components/layout/route-scroll-restore";
import { PageErrorBoundary } from "@/components/layout/error-boundary";
import {
  MobileDrawerProvider,
  useMobileDrawer,
} from "@/components/layout/mobile-drawer-context";
import { AuroraBackground } from "@/components/ui/aurora-background";
import { DefaultPasswordBanner } from "@/components/admin/default-password-banner";
import {
  MustChangePasswordProvider,
} from "@/components/admin/must-change-password-context";
import { MustChangePasswordGuard } from "@/components/admin/must-change-password-guard";
import { ActiveProfileProvider } from "@/lib/context/active-profile";
import { cn } from "@/lib/utils";

/**
 * Admin route group layout — Linear-style two-column shell.
 *
 * Structure:
 *   ┌──────────────┬───────────────────────────────────────────────┐
 *   │  Sidebar     │  TopNav                                       │
 *   │  (240/56)    ├───────────────────────────────────────────────┤
 *   │              │  <PageTransition>{children}</PageTransition>  │
 *   └──────────────┴───────────────────────────────────────────────┘
 *
 * Auth guard: on mount we `GET /admin/me`; if 401 we replace to
 * `/login?redirect=<pathname>`. Any other failure is treated as
 * "best-effort authenticated" so a transient gateway blip doesn't bounce
 * the user back to login.
 */
export default function AdminLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  const router = useRouter();
  const pathname = usePathname();
  const [state, setState] = useState<
    | { kind: "checking" }
    | { kind: "authenticated"; session: AdminSession }
    | { kind: "redirecting" }
  >({ kind: "checking" });

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const session = await getSession();
        if (cancelled) return;
        if (session === null) {
          setState({ kind: "redirecting" });
          const target = `/login?redirect=${encodeURIComponent(pathname ?? "/")}`;
          router.replace(target);
          return;
        }
        setState({ kind: "authenticated", session });
      } catch {
        if (cancelled) return;
        setState({
          kind: "authenticated",
          session: {
            user: "unknown",
            created_at: new Date().toISOString(),
            expires_at: new Date().toISOString(),
          },
        });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [pathname, router]);

  if (state.kind !== "authenticated") {
    return <div className="flex min-h-dvh items-center justify-center" />;
  }

  return (
    <MobileDrawerProvider>
      <MustChangePasswordProvider session={state.session}>
        <ActiveProfileProvider>
          <AdminShell user={state.session.user} pathname={pathname ?? "/"}>
            <MustChangePasswordGuard>{children}</MustChangePasswordGuard>
          </AdminShell>
        </ActiveProfileProvider>
      </MustChangePasswordProvider>
    </MobileDrawerProvider>
  );
}

/**
 * Inner shell — rendered under the <MobileDrawerProvider> so the backdrop
 * + sidebar can share drawer state. Close-on-route-change lives here
 * because the hook only works under the provider.
 */
function AdminShell({
  user,
  pathname,
  children,
}: {
  user: string;
  pathname: string;
  children: React.ReactNode;
}) {
  const { open: drawerOpen, setOpen: setDrawerOpen } = useMobileDrawer();

  // Close the drawer on route change — intentional UX cue that the tap on
  // a nav item did navigate somewhere.
  useEffect(() => {
    setDrawerOpen(false);
  }, [pathname, setDrawerOpen]);

  return (
    <div className="relative flex min-h-dvh gap-2 p-2 md:gap-4 md:p-4">
      {/* Tidepool aurora — fixed behind all admin content. Reads
          --tp-aurora-* / --tp-bg-* so it retints on theme flip. */}
      <AuroraBackground />
      <RouteScrollRestore />

      {/* Mobile drawer backdrop — only visible on <md when the sidebar
          drawer is open. Tapping it closes the drawer. */}
      <button
        type="button"
        aria-label="Close navigation"
        onClick={() => setDrawerOpen(false)}
        className={cn(
          "fixed inset-0 z-40 bg-black/50 backdrop-blur-sm md:hidden",
          "transition-opacity duration-200",
          drawerOpen
            ? "opacity-100"
            : "pointer-events-none opacity-0",
        )}
      />

      <Sidebar user={user} />

      <div className="flex min-w-0 flex-1 flex-col gap-2 md:gap-4">
        <TopNav />
        {/* Wave 1.3 — top-of-shell alert when the admin is still on the
            default `admin/root` seed. Renders nothing once the flag
            flips, so it's free for already-rotated installs. */}
        <DefaultPasswordBanner />
        <main className="relative flex flex-1 flex-col">
          <div className="mx-auto w-full max-w-[1440px] flex-1 space-y-6">
            <PageTransition>
              <PageErrorBoundary>{children}</PageErrorBoundary>
            </PageTransition>
          </div>
        </main>
      </div>
    </div>
  );
}
