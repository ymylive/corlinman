"use client";

import { useEffect, useState } from "react";
import { usePathname, useRouter } from "next/navigation";

import { getSession, type AdminSession } from "@/lib/auth";
import { TopNav } from "@/components/layout/nav";
import { Sidebar } from "@/components/layout/sidebar";

/**
 * Admin route group layout — shared TopNav + Sidebar across every
 * /plugins, /agents, /rag, /channels/qq, /scheduler, /approvals, /logs,
 * /config, /models page (plan §4).
 *
 * S5 T1: adds a client-side auth guard. On mount we hit `GET /admin/me`;
 * if it returns 401 we `router.replace('/login?redirect=<pathname>')`.
 * While the check is in flight we render nothing so a flash of the
 * authenticated layout never leaks for unauthenticated users.
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
        // Any non-401 failure (e.g. network down) — stay on the page and
        // let the child route render its own error. Treat as "best effort
        // assume authenticated" to avoid bouncing to /login on a blip.
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
    <div className="flex min-h-dvh flex-col">
      <TopNav user={state.session.user} />
      <div className="flex flex-1">
        <Sidebar />
        <main className="flex-1 space-y-6 p-6">{children}</main>
      </div>
    </div>
  );
}
