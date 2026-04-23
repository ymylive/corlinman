"use client";

import { useEffect, useState } from "react";
import { usePathname, useRouter } from "next/navigation";

import { getSession, type AdminSession } from "@/lib/auth";
import { TopNav } from "@/components/layout/nav";
import { Sidebar } from "@/components/layout/sidebar";
import { PageTransition } from "@/components/layout/page-transition";
import { RouteScrollRestore } from "@/components/layout/route-scroll-restore";
import { PageErrorBoundary } from "@/components/layout/error-boundary";
import { AuroraBackground } from "@/components/ui/aurora-background";

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
    <div className="relative flex min-h-dvh gap-4 p-4">
      {/* Tidepool aurora — fixed behind all admin content. Reads
          --tp-aurora-* / --tp-bg-* so it retints on theme flip. */}
      <AuroraBackground />
      <RouteScrollRestore />
      <Sidebar user={state.session.user} />
      <div className="flex min-w-0 flex-1 flex-col gap-4">
        <TopNav />
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
