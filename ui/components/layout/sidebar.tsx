"use client";

import * as React from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { motion } from "framer-motion";
import { useTranslation } from "react-i18next";
import {
  Activity,
  Bot,
  Boxes,
  ChevronsLeft,
  ChevronsRight,
  ClipboardCheck,
  Database,
  FileTerminal,
  LogOut,
  MessageCircle,
  Route,
  Settings,
  Timer,
} from "lucide-react";
import { toast } from "sonner";

import { cn } from "@/lib/utils";
import { logout } from "@/lib/auth";
import { BrandMark } from "./brand-mark";

interface NavItem {
  href: string;
  labelKey: string;
  icon: React.ComponentType<{ className?: string }>;
}

const ITEMS: NavItem[] = [
  { href: "/", labelKey: "nav.dashboard", icon: Activity },
  { href: "/plugins", labelKey: "nav.plugins", icon: Boxes },
  { href: "/agents", labelKey: "nav.agents", icon: Bot },
  { href: "/rag", labelKey: "nav.rag", icon: Database },
  { href: "/channels/qq", labelKey: "nav.channels", icon: MessageCircle },
  { href: "/scheduler", labelKey: "nav.scheduler", icon: Timer },
  { href: "/approvals", labelKey: "nav.approvals", icon: ClipboardCheck },
  { href: "/models", labelKey: "nav.models", icon: Route },
  { href: "/config", labelKey: "nav.config", icon: Settings },
  { href: "/logs", labelKey: "nav.logs", icon: FileTerminal },
];

const COLLAPSE_KEY = "corlinman.sidebar.collapsed.v1";

function readCollapsed(): boolean {
  if (typeof window === "undefined") return false;
  try {
    return window.localStorage.getItem(COLLAPSE_KEY) === "1";
  } catch {
    return false;
  }
}
function writeCollapsed(v: boolean): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(COLLAPSE_KEY, v ? "1" : "0");
  } catch {
    /* ignore */
  }
}

interface SidebarProps {
  user?: string;
}

export function Sidebar({ user }: SidebarProps) {
  const pathname = usePathname() ?? "/";
  const router = useRouter();
  const { t } = useTranslation();
  const [collapsed, setCollapsed] = React.useState(false);
  const [hydrated, setHydrated] = React.useState(false);
  const [loggingOut, setLoggingOut] = React.useState(false);

  React.useEffect(() => {
    setCollapsed(readCollapsed());
    setHydrated(true);
  }, []);

  const toggle = () => {
    setCollapsed((prev) => {
      const next = !prev;
      writeCollapsed(next);
      return next;
    });
  };

  async function onLogout() {
    setLoggingOut(true);
    try {
      await logout();
      toast.success(t("auth.logoutSuccess"));
    } catch {
      /* idempotent */
    } finally {
      router.push("/login");
    }
  }

  const width = collapsed && hydrated ? "w-[56px]" : "w-[240px]";

  return (
    <aside
      className={cn(
        "flex shrink-0 flex-col border-r border-border bg-surface/60 transition-[width] duration-200 ease-out",
        width,
      )}
      aria-label={t("nav.dashboard")}
    >
      {/* brand + collapse */}
      <div className="flex h-14 items-center justify-between border-b border-border px-3">
        <Link href="/" className="flex items-center gap-2 overflow-hidden">
          <BrandMark compact={collapsed && hydrated} />
        </Link>
        <button
          type="button"
          onClick={toggle}
          aria-label={
            collapsed ? t("nav.expandSidebar") : t("nav.collapseSidebar")
          }
          className="inline-flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
        >
          {collapsed ? (
            <ChevronsRight className="h-4 w-4" />
          ) : (
            <ChevronsLeft className="h-4 w-4" />
          )}
        </button>
      </div>

      <nav className="flex flex-1 flex-col gap-0.5 p-2">
        {ITEMS.map((item) => {
          const active =
            item.href === "/"
              ? pathname === "/"
              : pathname === item.href ||
                pathname.startsWith(`${item.href}/`);
          const Icon = item.icon;
          const label = t(item.labelKey);
          return (
            <Link
              key={item.href}
              href={item.href as never}
              className={cn(
                "relative flex items-center gap-3 rounded-md px-2.5 py-2 text-sm transition-colors",
                active
                  ? "bg-accent/70 text-accent-foreground"
                  : "text-muted-foreground hover:bg-accent/40 hover:text-foreground",
                collapsed && hydrated && "justify-center px-0",
              )}
              aria-current={active ? "page" : undefined}
              title={collapsed ? label : undefined}
            >
              {active ? (
                <motion.span
                  layoutId="sidebar-indicator"
                  className="absolute left-0 top-1 bottom-1 w-[2px] rounded-full bg-primary"
                  transition={{
                    type: "spring",
                    stiffness: 500,
                    damping: 40,
                    mass: 0.6,
                  }}
                />
              ) : null}
              <Icon className="h-4 w-4 shrink-0" />
              {collapsed && hydrated ? null : (
                <span className="truncate">{label}</span>
              )}
            </Link>
          );
        })}
      </nav>

      {/* user chip + footer */}
      <div className="border-t border-border p-3">
        {collapsed && hydrated ? (
          <button
            type="button"
            onClick={onLogout}
            aria-label={t("auth.logoutLabel")}
            disabled={loggingOut}
            className="flex h-8 w-full items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground disabled:opacity-50"
            data-testid="logout-button"
          >
            <LogOut className="h-4 w-4" />
          </button>
        ) : (
          <div className="flex items-center gap-2">
            <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-primary/15 text-[11px] font-semibold text-primary">
              {(user ?? "a").slice(0, 1).toUpperCase()}
            </div>
            <div className="min-w-0 flex-1 leading-tight">
              <div
                className="truncate text-xs font-medium text-foreground"
                data-testid="nav-user"
              >
                {user ?? "admin"}
              </div>
              <div className="truncate font-mono text-[10px] text-muted-foreground">
                v0.1.1
              </div>
            </div>
            <button
              type="button"
              onClick={onLogout}
              disabled={loggingOut}
              aria-label={t("auth.logoutLabel")}
              className="inline-flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground disabled:opacity-50"
              data-testid="logout-button"
            >
              <LogOut className="h-3.5 w-3.5" />
            </button>
          </div>
        )}
      </div>
    </aside>
  );
}
