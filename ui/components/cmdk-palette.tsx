"use client";

/**
 * Global ⌘K command palette. Powered by `cmdk` (built-in fuzzy match).
 *
 * Actions:
 *   - Jump to any admin route (10 destinations, synced with the sidebar list).
 *   - Toggle theme.
 *   - Switch language (zh-CN ↔ en).
 *   - Log out (POST /admin/logout via lib/auth).
 *   - Open a lightweight "Test chat" drawer that POSTs /v1/chat/completions.
 *   - Surface recent commands (top 5, persisted in localStorage).
 *
 * Context API exposed via <CommandPaletteProvider>:
 *   const { open, setOpen, toggle } = useCommandPalette();
 *
 * The topnav's "Search... ⌘K" pill calls `toggle()` on click. The keyboard
 * listener in <CommandPaletteProvider> handles ⌘K / Ctrl+K globally.
 */

import * as React from "react";
import { usePathname, useRouter } from "next/navigation";
import { useTheme } from "next-themes";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

import {
  CommandPalette as TidepoolCommandPalette,
  type PaletteGroup,
  type PaletteItem,
} from "@/components/ui/command-palette";
import {
  Activity,
  Bot,
  Boxes,
  ClipboardCheck,
  Command as CommandIcon,
  Database,
  FileTerminal,
  FilterX,
  Languages,
  Leaf,
  LogOut,
  MessageCircle,
  MessageSquare,
  Moon,
  Plug,
  RefreshCw,
  Route,
  Send,
  Settings,
  Sparkles,
  Sun,
  Timer,
  UserSquare,
  Wrench,
  Zap,
} from "lucide-react";

import { logout } from "@/lib/auth";
import { GATEWAY_BASE_URL, MOCK_API_URL } from "@/lib/api";
import { cn } from "@/lib/utils";
import { useMotion } from "@/components/ui/motion-safe";
import { useRecentRoutes } from "@/lib/hooks/use-recent-routes";

// --- context ----------------------------------------------------------------

interface Ctx {
  open: boolean;
  setOpen: (v: boolean) => void;
  toggle: () => void;
}

const CommandPaletteCtx = React.createContext<Ctx | null>(null);

export function useCommandPalette(): Ctx {
  const ctx = React.useContext(CommandPaletteCtx);
  if (!ctx)
    throw new Error(
      "useCommandPalette must be used inside <CommandPaletteProvider />",
    );
  return ctx;
}

// --- recent commands (localStorage) ----------------------------------------

const RECENT_KEY = "corlinman.cmdk.recent.v1";
const RECENT_MAX = 5;

function readRecent(): string[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(RECENT_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.slice(0, RECENT_MAX) : [];
  } catch {
    return [];
  }
}
function pushRecent(id: string): void {
  if (typeof window === "undefined") return;
  try {
    const prev = readRecent().filter((x) => x !== id);
    const next = [id, ...prev].slice(0, RECENT_MAX);
    window.localStorage.setItem(RECENT_KEY, JSON.stringify(next));
  } catch {
    /* ignore */
  }
}

// --- nav registry (kept in sync with the sidebar) --------------------------

interface NavCmd {
  id: string;
  labelKey: string;
  href: string;
  icon: React.ComponentType<{ className?: string }>;
  keywords?: string;
}

const NAV_CMDS: NavCmd[] = [
  { id: "nav.dashboard", labelKey: "nav.dashboard", href: "/", icon: Activity, keywords: "overview home 仪表盘 dashboard" },
  { id: "nav.plugins", labelKey: "nav.plugins", href: "/plugins", icon: Boxes, keywords: "tools manifest 插件" },
  { id: "nav.skills", labelKey: "nav.skills", href: "/skills", icon: Wrench, keywords: "skills gallery 技能" },
  { id: "nav.agents", labelKey: "nav.agents", href: "/agents", icon: Bot, keywords: "prompt editor agent" },
  { id: "nav.characters", labelKey: "nav.characters", href: "/characters", icon: UserSquare, keywords: "characters cards 角色卡" },
  { id: "nav.rag", labelKey: "nav.rag", href: "/rag", icon: Database, keywords: "retrieval chunks embeddings 向量" },
  { id: "nav.qq", labelKey: "nav.qq", href: "/channels/qq", icon: MessageCircle, keywords: "channels messaging 通道 qq" },
  { id: "nav.telegram", labelKey: "nav.telegram", href: "/channels/telegram", icon: Send, keywords: "telegram channel 电报" },
  { id: "nav.scheduler", labelKey: "nav.scheduler", href: "/scheduler", icon: Timer, keywords: "cron jobs 定时任务" },
  { id: "nav.approvals", labelKey: "nav.approvals", href: "/approvals", icon: ClipboardCheck, keywords: "pending tool gate 审批" },
  { id: "nav.evolution", labelKey: "nav.evolution", href: "/evolution", icon: Leaf, keywords: "evolution proposals self-improve 演化 自我改进" },
  { id: "nav.models", labelKey: "nav.models", href: "/models", icon: Route, keywords: "providers aliases routing 模型" },
  { id: "nav.providers", labelKey: "nav.providers", href: "/providers", icon: Plug, keywords: "providers llm openai" },
  { id: "nav.embedding", labelKey: "nav.embedding", href: "/embedding", icon: Sparkles, keywords: "embedding vectors 向量" },
  { id: "nav.config", labelKey: "nav.config", href: "/config", icon: Settings, keywords: "toml settings 配置" },
  { id: "nav.logs", labelKey: "nav.logs", href: "/logs", icon: FileTerminal, keywords: "stream events trace 日志" },
  { id: "nav.hooks", labelKey: "nav.hooks", href: "/hooks", icon: Zap, keywords: "hooks events monitor" },
];

// --- provider ---------------------------------------------------------------

export function CommandPaletteProvider({
  children,
}: {
  children: React.ReactNode;
}) {
  const [open, setOpen] = React.useState(false);
  const toggle = React.useCallback(() => setOpen((v) => !v), []);
  const pathname = usePathname();
  const { record } = useRecentRoutes();

  // Hotkeys: Cmd/Ctrl+K always; `?` only when not typing in an input.
  React.useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.key === "k" || e.key === "K") && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        toggle();
        return;
      }
      if (e.key === "?" && !e.metaKey && !e.ctrlKey && !e.altKey) {
        const t = e.target as HTMLElement | null;
        if (
          t &&
          (t.tagName === "INPUT" ||
            t.tagName === "TEXTAREA" ||
            t.tagName === "SELECT" ||
            t.isContentEditable)
        ) {
          return;
        }
        e.preventDefault();
        toggle();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [toggle]);

  // Track visited admin routes for the Recent section.
  React.useEffect(() => {
    if (pathname) record(pathname);
  }, [pathname, record]);

  return (
    <CommandPaletteCtx.Provider value={{ open, setOpen, toggle }}>
      {children}
      <CommandPalette open={open} setOpen={setOpen} />
    </CommandPaletteCtx.Provider>
  );
}

// --- palette UI -------------------------------------------------------------

function CommandPalette({
  open,
  setOpen,
}: {
  open: boolean;
  setOpen: (v: boolean) => void;
}) {
  const router = useRouter();
  const { theme, setTheme } = useTheme();
  const { t, i18n } = useTranslation();
  const { routes: recentRoutes } = useRecentRoutes();
  const [recent, setRecent] = React.useState<string[]>([]);
  const [chatOpen, setChatOpen] = React.useState(false);

  React.useEffect(() => {
    if (open) setRecent(readRecent());
  }, [open]);

  // defer side effect so the palette closes before navigation
  const defer = (fn: () => void) => requestAnimationFrame(fn);

  const navByHref = React.useMemo(() => {
    const m = new Map<string, NavCmd>();
    for (const n of NAV_CMDS) m.set(n.href, n);
    return m;
  }, []);
  const navById = React.useMemo(() => {
    const m = new Map<string, NavCmd>();
    for (const n of NAV_CMDS) m.set(n.id, n);
    return m;
  }, []);

  // Prefer route-history (any visited admin path) and fall back to legacy
  // per-id recents so existing users keep their list after this upgrade.
  const recentEntries = React.useMemo(() => {
    const out: { key: string; nav: NavCmd }[] = [];
    const seen = new Set<string>();
    for (const href of recentRoutes) {
      const n = navByHref.get(href);
      if (n && !seen.has(n.id)) {
        out.push({ key: `route-${href}`, nav: n });
        seen.add(n.id);
      }
    }
    for (const rid of recent) {
      const n = navById.get(rid);
      if (n && !seen.has(n.id)) {
        out.push({ key: `id-${rid}`, nav: n });
        seen.add(n.id);
      }
    }
    return out.slice(0, 5);
  }, [recentRoutes, recent, navByHref, navById]);

  // Build the PaletteGroup[] consumed by the new Tidepool primitive.
  const groups = React.useMemo<PaletteGroup[]>(() => {
    const gs: PaletteGroup[] = [];

    if (recentEntries.length > 0) {
      gs.push({
        id: "recent",
        label: t("cmdk.groupRecent"),
        items: recentEntries.map(({ key, nav: n }): PaletteItem => {
          const Icon = n.icon;
          return {
            id: `recent-${key}`,
            label: t(n.labelKey),
            icon: <Icon className="h-4 w-4" />,
            meta: n.href,
            keywords: ["recent", n.keywords ?? ""],
            onRun: () => {
              pushRecent(n.id);
              defer(() => router.push(n.href as never));
            },
          };
        }),
      });
    }

    gs.push({
      id: "navigate",
      label: t("cmdk.groupNavigate"),
      items: NAV_CMDS.map((n): PaletteItem => {
        const Icon = n.icon;
        return {
          id: n.id,
          label: t(n.labelKey),
          icon: <Icon className="h-4 w-4" />,
          meta: n.href,
          keywords: n.keywords ? [n.keywords] : [],
          onRun: () => {
            pushRecent(n.id);
            defer(() => router.push(n.href as never));
          },
        };
      }),
    });

    gs.push({
      id: "actions",
      label: t("cmdk.groupActions"),
      items: [
        {
          id: "action.chat",
          label: t("cmdk.testChat"),
          icon: <MessageSquare className="h-4 w-4" />,
          meta: t("cmdk.testChatHint"),
          keywords: ["test", "chat", "completion", "测试"],
          onRun: () => {
            pushRecent("action.chat");
            defer(() => setChatOpen(true));
          },
        },
        {
          id: "action.theme",
          label: theme === "dark" ? t("nav.switchToLight") : t("nav.switchToDark"),
          icon:
            theme === "dark" ? (
              <Sun className="h-4 w-4" />
            ) : (
              <Moon className="h-4 w-4" />
            ),
          shortcut: "⇧⌘L",
          keywords: ["toggle", "theme", "dark", "light", "主题"],
          onRun: () => {
            pushRecent("action.theme");
            defer(() => setTheme(theme === "dark" ? "light" : "dark"));
          },
        },
        {
          id: "action.language",
          label: t("cmdk.switchLanguage"),
          icon: <Languages className="h-4 w-4" />,
          meta: t("cmdk.switchLanguageHint"),
          keywords: ["i18n", "语言", "chinese", "english"],
          onRun: () => {
            pushRecent("action.language");
            defer(() => {
              const next = i18n.language?.startsWith("zh") ? "en" : "zh-CN";
              void i18n.changeLanguage(next);
            });
          },
        },
        {
          id: "action.reload-config",
          label: t("cmdk.reloadConfig"),
          icon: <RefreshCw className="h-4 w-4" />,
          meta: t("cmdk.reloadConfigHint"),
          keywords: ["reload", "refresh", "toml", "重载"],
          onRun: () => {
            pushRecent("action.reload-config");
            defer(() => toast.success(t("cmdk.reloadConfig")));
          },
        },
        {
          id: "action.clear-filter",
          label: t("cmdk.clearFilter"),
          icon: <FilterX className="h-4 w-4" />,
          meta: t("cmdk.clearFilterHint"),
          keywords: ["clear", "filter", "reset", "清除筛选"],
          onRun: () => {
            pushRecent("action.clear-filter");
            defer(() => {
              window.dispatchEvent(new CustomEvent("corlinman.filter.clear"));
              toast.success(t("cmdk.clearFilter"));
            });
          },
        },
        {
          id: "action.logout",
          label: t("cmdk.logout"),
          icon: <LogOut className="h-4 w-4" />,
          meta: t("cmdk.logoutHint"),
          keywords: ["logout", "sign out", "退出"],
          onRun: () => {
            pushRecent("action.logout");
            defer(async () => {
              try {
                await logout();
                toast.success(t("auth.logoutSuccess"));
              } catch {
                /* idempotent */
              } finally {
                router.push("/login");
              }
            });
          },
        },
      ],
    });

    return gs;
  }, [recentEntries, theme, router, setTheme, t, i18n]);

  return (
    <>
      <TidepoolCommandPalette
        open={open}
        onOpenChange={setOpen}
        groups={groups}
        placeholder={t("cmdk.searchPlaceholder")}
      />
      {chatOpen ? (
        <TestChatDrawer onClose={() => setChatOpen(false)} />
      ) : null}
    </>
  );
}

// --- test chat drawer -------------------------------------------------------

function TestChatDrawer({ onClose }: { onClose: () => void }) {
  const { t } = useTranslation();
  const [prompt, setPrompt] = React.useState("Hello!");
  const [answer, setAnswer] = React.useState<string>("");
  const [submitting, setSubmitting] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    setAnswer("");
    try {
      const base = MOCK_API_URL || GATEWAY_BASE_URL;
      const res = await fetch(`${base}/v1/chat/completions`, {
        method: "POST",
        credentials: MOCK_API_URL ? "omit" : "include",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          model: "default",
          messages: [{ role: "user", content: prompt }],
          stream: false,
        }),
      });
      if (!res.ok) {
        setError(`${res.status} ${res.statusText}`);
      } else {
        const data = await res.json().catch(() => ({}));
        const choice =
          (data as { choices?: Array<{ message?: { content?: string } }> })
            .choices?.[0]?.message?.content ?? JSON.stringify(data);
        setAnswer(String(choice));
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div
      role="dialog"
      aria-modal="true"
      className="fixed inset-0 z-[60] flex items-start justify-center px-4 pt-[10vh]"
    >
      <div
        className="absolute inset-0 bg-black/60 backdrop-blur-sm animate-in fade-in-0 duration-150"
        onClick={onClose}
        aria-hidden
      />
      <div
        className={cn(
          "relative z-10 flex w-full max-w-2xl flex-col gap-3 rounded-lg border border-border bg-popover p-4 shadow-2xl",
          "animate-in fade-in-0 zoom-in-95 duration-150",
        )}
      >
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold">{t("cmdk.testChatTitle")}</h2>
          <kbd className="rounded border border-border bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">
            ESC
          </kbd>
        </div>
        <form onSubmit={submit} className="space-y-2">
          <textarea
            className="w-full rounded-md border border-input bg-background p-2 font-mono text-xs outline-none focus-visible:ring-1 focus-visible:ring-ring"
            rows={3}
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
          />
          <div className="flex items-center justify-between">
            <span className="text-xs text-muted-foreground">
              {t("cmdk.testChatHintInline")}
            </span>
            <button
              type="submit"
              disabled={submitting || !prompt.trim()}
              className="inline-flex h-8 items-center rounded-md bg-primary px-3 text-xs font-medium text-primary-foreground transition-colors hover:bg-primary/90 disabled:opacity-50"
            >
              {submitting ? t("cmdk.sending") : t("cmdk.send")}
            </button>
          </div>
        </form>
        {error ? (
          <p className="rounded-md border border-destructive/40 bg-destructive/10 p-2 text-xs text-destructive-foreground">
            {error}
          </p>
        ) : null}
        {answer ? (
          <pre className="max-h-[40vh] overflow-auto rounded-md border border-border bg-surface p-3 font-mono text-xs">
            {answer}
          </pre>
        ) : null}
      </div>
    </div>
  );
}
