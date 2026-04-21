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
import { Command } from "cmdk";
import { useRouter } from "next/navigation";
import { useTheme } from "next-themes";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import {
  Activity,
  Bot,
  Boxes,
  ClipboardCheck,
  Command as CommandIcon,
  Database,
  FileTerminal,
  Languages,
  LogOut,
  MessageCircle,
  MessageSquare,
  Moon,
  Route,
  Settings,
  Sun,
  Timer,
} from "lucide-react";

import { logout } from "@/lib/auth";
import { GATEWAY_BASE_URL, MOCK_API_URL } from "@/lib/api";
import { cn } from "@/lib/utils";

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
  { id: "nav.agents", labelKey: "nav.agents", href: "/agents", icon: Bot, keywords: "prompt editor agent" },
  { id: "nav.rag", labelKey: "nav.rag", href: "/rag", icon: Database, keywords: "retrieval chunks embeddings 向量" },
  { id: "nav.qq", labelKey: "nav.qq", href: "/channels/qq", icon: MessageCircle, keywords: "channels messaging 通道 qq" },
  { id: "nav.scheduler", labelKey: "nav.scheduler", href: "/scheduler", icon: Timer, keywords: "cron jobs 定时任务" },
  { id: "nav.approvals", labelKey: "nav.approvals", href: "/approvals", icon: ClipboardCheck, keywords: "pending tool gate 审批" },
  { id: "nav.models", labelKey: "nav.models", href: "/models", icon: Route, keywords: "providers aliases routing 模型" },
  { id: "nav.config", labelKey: "nav.config", href: "/config", icon: Settings, keywords: "toml settings 配置" },
  { id: "nav.logs", labelKey: "nav.logs", href: "/logs", icon: FileTerminal, keywords: "stream events trace 日志" },
];

// --- provider ---------------------------------------------------------------

export function CommandPaletteProvider({
  children,
}: {
  children: React.ReactNode;
}) {
  const [open, setOpen] = React.useState(false);
  const toggle = React.useCallback(() => setOpen((v) => !v), []);

  React.useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.key === "k" || e.key === "K") && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        toggle();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [toggle]);

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
  const [recent, setRecent] = React.useState<string[]>([]);
  const [chatOpen, setChatOpen] = React.useState(false);

  React.useEffect(() => {
    if (open) setRecent(readRecent());
  }, [open]);

  const run = (id: string, fn: () => void) => {
    pushRecent(id);
    setOpen(false);
    // defer side effect so the palette closes before navigation
    requestAnimationFrame(() => fn());
  };

  const navById = React.useMemo(() => {
    const m = new Map<string, NavCmd>();
    for (const n of NAV_CMDS) m.set(n.id, n);
    return m;
  }, []);

  if (!open && !chatOpen) return null;

  return (
    <>
      {open ? (
        <div
          role="dialog"
          aria-modal="true"
          className="fixed inset-0 z-[60] flex items-start justify-center px-4 pt-[15vh]"
        >
          {/* blurred backdrop */}
          <div
            className="absolute inset-0 bg-black/60 backdrop-blur-sm animate-in fade-in-0 duration-150"
            onClick={() => setOpen(false)}
            aria-hidden
          />
          <div
            className={cn(
              "relative z-10 w-full max-w-[640px] overflow-hidden rounded-lg border border-border bg-popover text-popover-foreground shadow-2xl",
              "animate-in fade-in-0 zoom-in-95 duration-150",
            )}
          >
            <Command label={t("cmdk.commandMenu")} loop>
              <div className="flex items-center gap-2 border-b border-border px-3">
                <CommandIcon className="h-4 w-4 text-muted-foreground" />
                <Command.Input
                  autoFocus
                  placeholder={t("cmdk.searchPlaceholder")}
                  className="h-11 w-full bg-transparent text-sm outline-none placeholder:text-muted-foreground"
                />
                <kbd className="rounded border border-border bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">
                  ESC
                </kbd>
              </div>
              <Command.List className="max-h-[360px] overflow-y-auto p-1">
                <Command.Empty className="px-3 py-6 text-center text-sm text-muted-foreground">
                  {t("cmdk.noResults")}
                </Command.Empty>

                {recent.length > 0 ? (
                  <Command.Group
                    heading={t("cmdk.groupRecent")}
                    className="[&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:py-1.5 [&_[cmdk-group-heading]]:text-[10px] [&_[cmdk-group-heading]]:uppercase [&_[cmdk-group-heading]]:tracking-wider [&_[cmdk-group-heading]]:text-muted-foreground"
                  >
                    {recent.map((rid) => {
                      const n = navById.get(rid);
                      if (!n) return null;
                      const Icon = n.icon;
                      const label = t(n.labelKey);
                      return (
                        <PaletteItem
                          key={`recent-${rid}`}
                          value={`recent ${label} ${n.keywords ?? ""}`}
                          onSelect={() =>
                            run(n.id, () => router.push(n.href as never))
                          }
                          icon={<Icon className="h-4 w-4" />}
                          label={label}
                          hint={n.href}
                        />
                      );
                    })}
                  </Command.Group>
                ) : null}

                <Command.Group
                  heading={t("cmdk.groupNavigate")}
                  className="[&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:py-1.5 [&_[cmdk-group-heading]]:text-[10px] [&_[cmdk-group-heading]]:uppercase [&_[cmdk-group-heading]]:tracking-wider [&_[cmdk-group-heading]]:text-muted-foreground"
                >
                  {NAV_CMDS.map((n) => {
                    const Icon = n.icon;
                    const label = t(n.labelKey);
                    return (
                      <PaletteItem
                        key={n.id}
                        value={`${label} ${n.keywords ?? ""}`}
                        onSelect={() =>
                          run(n.id, () => router.push(n.href as never))
                        }
                        icon={<Icon className="h-4 w-4" />}
                        label={label}
                        hint={n.href}
                      />
                    );
                  })}
                </Command.Group>

                <Command.Group
                  heading={t("cmdk.groupActions")}
                  className="[&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:py-1.5 [&_[cmdk-group-heading]]:text-[10px] [&_[cmdk-group-heading]]:uppercase [&_[cmdk-group-heading]]:tracking-wider [&_[cmdk-group-heading]]:text-muted-foreground"
                >
                  <PaletteItem
                    value="test chat completion 测试"
                    onSelect={() =>
                      run("action.chat", () => {
                        setChatOpen(true);
                      })
                    }
                    icon={<MessageSquare className="h-4 w-4" />}
                    label={t("cmdk.testChat")}
                    hint={t("cmdk.testChatHint")}
                  />
                  <PaletteItem
                    value="toggle theme dark light 主题"
                    onSelect={() =>
                      run("action.theme", () =>
                        setTheme(theme === "dark" ? "light" : "dark"),
                      )
                    }
                    icon={
                      theme === "dark" ? (
                        <Sun className="h-4 w-4" />
                      ) : (
                        <Moon className="h-4 w-4" />
                      )
                    }
                    label={
                      theme === "dark"
                        ? t("nav.switchToLight")
                        : t("nav.switchToDark")
                    }
                    hint="⇧⌘L"
                  />
                  <PaletteItem
                    value="switch language i18n 语言 chinese english 中英"
                    onSelect={() =>
                      run("action.language", () => {
                        const next = i18n.language?.startsWith("zh")
                          ? "en"
                          : "zh-CN";
                        i18n.changeLanguage(next);
                      })
                    }
                    icon={<Languages className="h-4 w-4" />}
                    label={t("cmdk.switchLanguage")}
                    hint={t("cmdk.switchLanguageHint")}
                  />
                  <PaletteItem
                    value="logout sign out 退出"
                    onSelect={() =>
                      run("action.logout", async () => {
                        try {
                          await logout();
                          toast.success(t("auth.logoutSuccess"));
                        } catch {
                          /* idempotent */
                        } finally {
                          router.push("/login");
                        }
                      })
                    }
                    icon={<LogOut className="h-4 w-4" />}
                    label={t("cmdk.logout")}
                    hint={t("cmdk.logoutHint")}
                  />
                </Command.Group>
              </Command.List>
            </Command>
          </div>
        </div>
      ) : null}

      {chatOpen ? (
        <TestChatDrawer onClose={() => setChatOpen(false)} />
      ) : null}
    </>
  );
}

function PaletteItem({
  value,
  onSelect,
  icon,
  label,
  hint,
}: {
  value: string;
  onSelect: () => void;
  icon: React.ReactNode;
  label: string;
  hint?: string;
}) {
  return (
    <Command.Item
      value={value}
      onSelect={onSelect}
      className={cn(
        "flex cursor-pointer select-none items-center gap-3 rounded-md px-2 py-2 text-sm outline-none",
        "data-[selected=true]:bg-accent data-[selected=true]:text-accent-foreground",
      )}
    >
      <span className="text-muted-foreground">{icon}</span>
      <span className="flex-1">{label}</span>
      {hint ? (
        <span className="font-mono text-[10px] text-muted-foreground">{hint}</span>
      ) : null}
    </Command.Item>
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
