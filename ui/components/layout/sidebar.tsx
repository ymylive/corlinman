"use client";

import * as React from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { motion } from "framer-motion";
import { useTranslation } from "react-i18next";
import {
  Activity,
  Beaker,
  BookOpen,
  Bot,
  Boxes,
  Building2,
  ChevronRight,
  ChevronsLeft,
  ChevronsRight,
  ClipboardCheck,
  Database,
  FileTerminal,
  Fingerprint,
  Frame,
  KeyRound,
  Leaf,
  LogOut,
  MessageCircle,
  MessagesSquare,
  Network,
  Plug,
  Radio,
  Route,
  Send,
  Settings,
  Share2,
  Sparkles,
  Timer,
  Users,
  UserSquare,
  Wrench,
  Zap,
} from "lucide-react";
import { toast } from "sonner";

import { cn } from "@/lib/utils";
import { logout } from "@/lib/auth";
import { useMotion } from "@/components/ui/motion-safe";
import { useMobileDrawer } from "./mobile-drawer-context";
import { BrandMark } from "./brand-mark";
import { ChangePasswordDialog } from "./change-password-dialog";

interface NavItem {
  kind?: "item";
  href: string;
  labelKey: string;
  icon: React.ComponentType<{ className?: string }>;
}

interface NavGroup {
  kind: "group";
  /** Stable id (used for local-storage + keyboard nav). */
  id: string;
  labelKey: string;
  icon: React.ComponentType<{ className?: string }>;
  children: NavItem[];
}

type NavEntry = NavItem | NavGroup;

const ITEMS: NavEntry[] = [
  { href: "/", labelKey: "nav.dashboard", icon: Activity },
  { href: "/plugins", labelKey: "nav.plugins", icon: Boxes },
  { href: "/skills", labelKey: "nav.skills", icon: Wrench },
  { href: "/agents", labelKey: "nav.agents", icon: Bot },
  { href: "/characters", labelKey: "nav.characters", icon: UserSquare },
  { href: "/diary", labelKey: "nav.diary", icon: BookOpen },
  { href: "/rag", labelKey: "nav.rag", icon: Database },
  {
    kind: "group",
    id: "channels",
    labelKey: "nav.channels",
    icon: Radio,
    children: [
      {
        href: "/channels/qq",
        labelKey: "nav.channelQq",
        icon: MessageCircle,
      },
      {
        href: "/channels/telegram",
        labelKey: "nav.channelTelegram",
        icon: Send,
      },
    ],
  },
  { href: "/scheduler", labelKey: "nav.scheduler", icon: Timer },
  { href: "/approvals", labelKey: "nav.approvals", icon: ClipboardCheck },
  { href: "/sessions", labelKey: "nav.sessions", icon: MessagesSquare },
  { href: "/identity", labelKey: "nav.identity", icon: Fingerprint },
  { href: "/evolution", labelKey: "nav.evolution", icon: Leaf },
  { href: "/models", labelKey: "nav.models", icon: Route },
  { href: "/providers", labelKey: "nav.providers", icon: Plug },
  { href: "/credentials", labelKey: "nav.credentials", icon: KeyRound },
  { href: "/newapi", labelKey: "nav.newapi", icon: Plug },
  { href: "/embedding", labelKey: "nav.embedding", icon: Sparkles },
  { href: "/tagmemo", labelKey: "nav.tagmemo", icon: Sparkles },
  { href: "/config", labelKey: "nav.config", icon: Settings },
  { href: "/tenants", labelKey: "nav.tenants", icon: Building2 },
  { href: "/profiles", labelKey: "nav.profiles", icon: Users },
  { href: "/federation", labelKey: "nav.federation", icon: Share2 },
  { href: "/logs", labelKey: "nav.logs", icon: FileTerminal },
  { href: "/hooks", labelKey: "nav.hooks", icon: Zap },
  { href: "/nodes", labelKey: "nav.nodes", icon: Network },
  { href: "/playground/protocol", labelKey: "nav.playground", icon: Beaker },
  { href: "/canvas", labelKey: "nav.canvas", icon: Frame },
];

function isActiveHref(pathname: string, href: string): boolean {
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(`${href}/`);
}

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
  const [changePasswordOpen, setChangePasswordOpen] = React.useState(false);
  const { open: drawerOpen } = useMobileDrawer();

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

  // Mobile always uses the expanded 240px width (the 72px collapsed mode
  // is a desktop affordance; in a drawer there's plenty of horizontal
  // room). Desktop follows the persisted `collapsed` preference.
  const width = collapsed && hydrated ? "md:w-[72px]" : "md:w-[240px]";

  return (
    <aside
      className={cn(
        // Tidepool: floating glass panel. On desktop it's a sticky flex
        // column in the admin layout row; on mobile (<md) it slides in
        // from the left over a backdrop driven by <MobileDrawerProvider>.
        "flex flex-col overflow-hidden rounded-2xl border",
        "bg-tp-glass border-tp-glass-edge",
        "backdrop-blur-glass backdrop-saturate-glass",
        "shadow-[inset_0_1px_0_var(--tp-glass-hl)] shadow-tp-panel",
        // Desktop ≥md: sticky inline flex member.
        "md:relative md:sticky md:top-4 md:self-start md:max-h-[calc(100dvh-2rem)]",
        "md:shrink-0 md:translate-x-0",
        "md:transition-[width] md:duration-200 md:ease-out",
        // Mobile <md: fixed slide-in drawer at 240px.
        "fixed inset-y-2 left-2 z-50 w-[240px] max-h-[calc(100dvh-16px)]",
        "transition-transform duration-200 ease-out",
        drawerOpen ? "translate-x-0" : "-translate-x-[calc(100%+12px)]",
        width,
      )}
      id="admin-sidebar"
      aria-label={t("nav.dashboard")}
      aria-hidden={
        // On mobile when the drawer is closed, take the aside out of the
        // accessibility tree so screen readers don't land on hidden nav.
        typeof window !== "undefined" &&
        window.matchMedia?.("(max-width: 767px)").matches &&
        !drawerOpen
          ? true
          : undefined
      }
    >
      {/* brand + collapse */}
      <div className="flex items-center justify-between gap-2 border-b border-tp-glass-edge px-3.5 py-3.5">
        <Link href="/" className="flex items-center gap-2 overflow-hidden">
          <BrandMarkNudge>
            <BrandMark compact={collapsed && hydrated} />
          </BrandMarkNudge>
        </Link>
        <button
          type="button"
          onClick={toggle}
          aria-label={
            collapsed ? t("nav.expandSidebar") : t("nav.collapseSidebar")
          }
          className="inline-flex h-7 w-7 items-center justify-center rounded-md text-tp-ink-3 transition-colors hover:bg-tp-glass-inner hover:text-tp-ink"
        >
          {collapsed ? (
            <ChevronsRight className="h-4 w-4" />
          ) : (
            <ChevronsLeft className="h-4 w-4" />
          )}
        </button>
      </div>

      <nav className="flex flex-1 flex-col gap-0.5 overflow-y-auto p-2">
        {ITEMS.map((entry) => {
          if (entry.kind === "group") {
            return (
              <SidebarGroup
                key={entry.id}
                group={entry}
                pathname={pathname}
                collapsed={collapsed && hydrated}
              />
            );
          }
          return (
            <SidebarItem
              key={entry.href}
              item={entry}
              pathname={pathname}
              collapsed={collapsed && hydrated}
            />
          );
        })}
      </nav>

      {/* user chip + footer */}
      <div className="border-t border-tp-glass-edge p-3">
        {collapsed && hydrated ? (
          <div className="flex flex-col items-center gap-1">
            <button
              type="button"
              onClick={() => setChangePasswordOpen(true)}
              aria-label={t("auth.changePasswordMenuItem")}
              className="flex h-8 w-full items-center justify-center rounded-md text-tp-ink-3 transition-colors hover:bg-tp-glass-inner hover:text-tp-ink"
              data-testid="change-password-button"
            >
              <KeyRound className="h-4 w-4" />
            </button>
            <button
              type="button"
              onClick={onLogout}
              aria-label={t("auth.logoutLabel")}
              disabled={loggingOut}
              className="flex h-8 w-full items-center justify-center rounded-md text-tp-ink-3 transition-colors hover:bg-tp-glass-inner hover:text-tp-ink disabled:opacity-50"
              data-testid="logout-button"
            >
              <LogOut className="h-4 w-4" />
            </button>
          </div>
        ) : (
          <div className="flex items-center gap-2">
            <div
              className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-[11px] font-semibold text-white"
              style={{
                background: "linear-gradient(135deg, var(--tp-amber), var(--tp-ember))",
                boxShadow: "0 0 10px -3px var(--tp-amber-glow)",
              }}
            >
              {(user ?? "a").slice(0, 1).toUpperCase()}
            </div>
            <div className="min-w-0 flex-1 leading-tight">
              <div
                className="truncate text-xs font-medium text-tp-ink"
                data-testid="nav-user"
              >
                {user ?? "admin"}
              </div>
              <div className="truncate font-mono text-[10px] text-tp-ink-3">
                v0.3.0
              </div>
            </div>
            <button
              type="button"
              onClick={() => setChangePasswordOpen(true)}
              aria-label={t("auth.changePasswordMenuItem")}
              title={t("auth.changePasswordMenuItem")}
              className="inline-flex h-7 w-7 items-center justify-center rounded-md text-tp-ink-3 transition-colors hover:bg-tp-glass-inner hover:text-tp-ink"
              data-testid="change-password-button"
            >
              <KeyRound className="h-3.5 w-3.5" />
            </button>
            <button
              type="button"
              onClick={onLogout}
              disabled={loggingOut}
              aria-label={t("auth.logoutLabel")}
              className="inline-flex h-7 w-7 items-center justify-center rounded-md text-tp-ink-3 transition-colors hover:bg-tp-glass-inner hover:text-tp-ink disabled:opacity-50"
              data-testid="logout-button"
            >
              <LogOut className="h-3.5 w-3.5" />
            </button>
          </div>
        )}
      </div>
      <ChangePasswordDialog
        open={changePasswordOpen}
        onOpenChange={setChangePasswordOpen}
      />
    </aside>
  );
}

/**
 * Single leaf entry. Extracted so group children can reuse the same visual
 * treatment as top-level items.
 */
function SidebarItem({
  item,
  pathname,
  collapsed,
  nested = false,
  onRef,
  onKeyDown,
}: {
  item: NavItem;
  pathname: string;
  collapsed: boolean;
  nested?: boolean;
  onRef?: (el: HTMLAnchorElement | null) => void;
  onKeyDown?: (e: React.KeyboardEvent<HTMLAnchorElement>) => void;
}) {
  const { t } = useTranslation();
  const active = isActiveHref(pathname, item.href);
  const Icon = item.icon;
  const label = t(item.labelKey);
  return (
    <Link
      ref={onRef}
      href={item.href as never}
      onKeyDown={onKeyDown}
      className={cn(
        "group relative flex items-center gap-2.5 rounded-lg px-2.5 py-1.5 text-[13px] transition-colors",
        // Tidepool: hover = text lift + a dim amber left hint (consistent
        // with the animated active indicator). No filled bg — a plain
        // rgba(255,255,255,0.04–0.08) rectangle reads as a stray layer on
        // top of the already-glass sidebar.
        active
          ? "text-tp-ink"
          : "text-tp-ink-2 hover:text-tp-ink",
        collapsed && "justify-center px-0",
        nested && !collapsed && "pl-8",
      )}
      aria-current={active ? "page" : undefined}
      title={collapsed ? label : undefined}
    >
      {active ? (
        <motion.span
          layoutId="sidebar-indicator"
          aria-hidden
          className="absolute left-[-6px] top-1/2 h-3.5 w-[3px] -translate-y-1/2 rounded-[2px]"
          style={{
            background: "linear-gradient(to bottom, var(--tp-amber), var(--tp-ember))",
            boxShadow: "0 0 8px var(--tp-amber-glow)",
          }}
          transition={{
            type: "spring",
            stiffness: 500,
            damping: 40,
            mass: 0.6,
          }}
        />
      ) : (
        // Dim amber tick that appears on hover only — previews the active
        // indicator without the layoutId dance (kept separate so it doesn't
        // fight the animated bar when the user hovers a sibling).
        <span
          aria-hidden
          className={cn(
            "pointer-events-none absolute left-[-6px] top-1/2 h-3 w-[2px] -translate-y-1/2 rounded-[2px]",
            "opacity-0 transition-opacity duration-150 group-hover:opacity-60",
          )}
          style={{
            background: "var(--tp-amber)",
          }}
        />
      )}
      <Icon className="h-[14px] w-[14px] shrink-0 opacity-80" />
      {collapsed ? null : <span className="truncate">{label}</span>}
    </Link>
  );
}

/**
 * Collapsible group. Defaults to collapsed; auto-expands when the current
 * route matches one of its children. Keyboard:
 *   - Enter / Space on the toggle flips expanded.
 *   - ArrowDown on the toggle moves focus to the first child.
 *   - ArrowUp on the first child returns focus to the toggle.
 */
function SidebarGroup({
  group,
  pathname,
  collapsed,
}: {
  group: NavGroup;
  pathname: string;
  collapsed: boolean;
}) {
  const { t } = useTranslation();
  const hasActiveChild = group.children.some((c) =>
    isActiveHref(pathname, c.href),
  );
  const [expanded, setExpanded] = React.useState<boolean>(hasActiveChild);

  // Auto-expand whenever the current route matches a child. Closing stays
  // user-driven — we don't force collapse when the route navigates away.
  React.useEffect(() => {
    if (hasActiveChild) setExpanded(true);
  }, [hasActiveChild]);

  const toggleRef = React.useRef<HTMLButtonElement | null>(null);
  const childRefs = React.useRef<Array<HTMLAnchorElement | null>>([]);

  const Icon = group.icon;
  const label = t(group.labelKey);

  const onToggleKeyDown = (e: React.KeyboardEvent<HTMLButtonElement>) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      setExpanded((v) => !v);
      return;
    }
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (!expanded) setExpanded(true);
      // Focus is deferred so the child list has a chance to mount.
      requestAnimationFrame(() => childRefs.current[0]?.focus());
    }
  };

  const onChildKeyDown = (
    e: React.KeyboardEvent<HTMLAnchorElement>,
    idx: number,
  ) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      childRefs.current[idx + 1]?.focus();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      if (idx === 0) {
        toggleRef.current?.focus();
      } else {
        childRefs.current[idx - 1]?.focus();
      }
    }
  };

  // Collapsed rail: render children as flat icon entries so every channel
  // remains one click away.
  if (collapsed) {
    return (
      <>
        {group.children.map((child) => (
          <SidebarItem
            key={child.href}
            item={child}
            pathname={pathname}
            collapsed
          />
        ))}
      </>
    );
  }

  return (
    <div
      role="group"
      aria-label={label}
      data-testid={`sidebar-group-${group.id}`}
    >
      <button
        ref={toggleRef}
        type="button"
        onClick={() => setExpanded((v) => !v)}
        onKeyDown={onToggleKeyDown}
        aria-expanded={expanded}
        aria-controls={`sidebar-group-${group.id}-list`}
        aria-label={label}
        data-testid={`sidebar-group-toggle-${group.id}`}
        className={cn(
          "relative flex w-full items-center gap-2.5 rounded-lg px-2.5 py-1.5 text-[13px] transition-colors",
          // Same "no filled-bg hover" rule as SidebarItem — full-width
          // rectangles on glass read as a stray layer.
          hasActiveChild
            ? "font-medium text-tp-ink"
            : "text-tp-ink-2 hover:text-tp-ink",
        )}
      >
        <Icon className="h-[14px] w-[14px] shrink-0 opacity-80" />
        <span className="truncate">{label}</span>
        <motion.span
          aria-hidden
          className="ml-auto inline-flex"
          animate={{ rotate: expanded ? 90 : 0 }}
          transition={{ duration: 0.15, ease: "easeOut" }}
        >
          <ChevronRight className="h-3 w-3 text-tp-ink-3" />
        </motion.span>
      </button>
      {expanded ? (
        <ul
          id={`sidebar-group-${group.id}-list`}
          className="mt-0.5 flex flex-col gap-0.5"
          role="list"
        >
          {group.children.map((child, idx) => (
            <li key={child.href}>
              <SidebarItem
                item={child}
                pathname={pathname}
                collapsed={false}
                nested
                onRef={(el) => {
                  childRefs.current[idx] = el;
                }}
                onKeyDown={(e) => onChildKeyDown(e, idx)}
              />
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

/**
 * Plays a 1° rotate + 2% scale nudge on the brand-mark whenever the route
 * changes. Visually tiny but signals "you moved" without competing with the
 * page-transition itself. Disabled under `prefers-reduced-motion`.
 */
function BrandMarkNudge({ children }: { children: React.ReactNode }) {
  const pathname = usePathname() ?? "/";
  const { reduced } = useMotion();
  // Monotonically increasing key drives the animate prop via the pathname.
  // `initial={false}` prevents a nudge on first mount.
  const animate = reduced
    ? { rotate: 0, scale: 1 }
    : { rotate: [0, 1, 0], scale: [1, 1.02, 1] };
  return (
    <motion.span
      key={pathname}
      className="inline-flex origin-center"
      initial={false}
      animate={animate}
      transition={{ duration: 0.3, ease: [0.34, 1.56, 0.64, 1] }}
    >
      {children}
    </motion.span>
  );
}
