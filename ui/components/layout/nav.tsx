"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { logout } from "@/lib/auth";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

interface TopNavProps {
  /** Logged-in admin user, displayed next to the logout button. */
  user?: string;
}

/**
 * Top bar. Shows product name, build channel badge, user name, and a
 * logout button (S5 T1). Theme + locale toggles still TODO.
 */
export function TopNav({ user }: TopNavProps) {
  const router = useRouter();
  const [loggingOut, setLoggingOut] = useState(false);

  async function onLogout() {
    setLoggingOut(true);
    try {
      await logout();
    } catch {
      // Logout is idempotent on the server; surface nothing on failure,
      // just send the user back to /login anyway.
    } finally {
      router.push("/login");
    }
  }

  return (
    <header className="flex h-14 items-center justify-between border-b border-border bg-background/95 px-6 backdrop-blur supports-[backdrop-filter]:bg-background/60">
      <div className="flex items-center gap-3">
        <Link href="/" className="font-semibold tracking-tight">
          corlinman
        </Link>
        <Badge variant="outline" className="text-xs">
          0.1.0 · M0
        </Badge>
      </div>
      <div className="flex items-center gap-2">
        <Button variant="ghost" size="sm" aria-label="theme" disabled>
          {/* TODO(M6): theme toggle via next-themes */}
          Theme
        </Button>
        <Button variant="ghost" size="sm" aria-label="locale" disabled>
          {/* TODO(M6): locale switcher (zh / en) */}
          中/EN
        </Button>
        {user ? (
          <span
            className="text-sm text-muted-foreground"
            data-testid="nav-user"
          >
            {user}
          </span>
        ) : null}
        <Button
          variant="outline"
          size="sm"
          onClick={onLogout}
          disabled={loggingOut}
          data-testid="logout-button"
        >
          {loggingOut ? "退出中..." : "退出登录"}
        </Button>
      </div>
    </header>
  );
}
