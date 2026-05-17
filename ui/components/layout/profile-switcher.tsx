"use client";

/**
 * Profile switcher (W3.4 — top-nav dropdown).
 *
 * Sits beside :func:`TenantSwitcher` in the admin top-nav. Shows the
 * currently active profile (slug + display_name fallback) and a
 * dropdown of every profile + a "Manage profiles…" link to ``/profiles``.
 *
 * Self-rolled disclosure (no ``@radix-ui/react-dropdown-menu`` in the
 * dep tree yet) — the listbox is rendered as an absolutely-positioned
 * panel below the trigger, dismissed on outside click / Escape /
 * blur-out. Keyboard:
 *
 *   * ``ArrowDown`` / ``ArrowUp``    — move focus through items
 *   * ``Enter``                       — activate the focused item
 *   * ``Escape``                      — close, return focus to trigger
 *
 * The dropdown ALWAYS includes the current operator's profiles even
 * when there's only one — the operator still needs the "Manage…" link
 * to reach the create flow.
 *
 * On switch we call :func:`useActiveProfile().setSlug`. No backend call
 * is fired; the slug is read by ``/chat`` / ``/skills`` pages later.
 */

import * as React from "react";
import { useRouter } from "next/navigation";
import { useTranslation } from "react-i18next";
import { ChevronDown, Check, Users, Settings } from "lucide-react";

import { useActiveProfile } from "@/lib/context/active-profile";
import { cn } from "@/lib/utils";

export interface ProfileSwitcherProps {
  className?: string;
}

export function ProfileSwitcher({
  className,
}: ProfileSwitcherProps): React.ReactElement {
  const { t } = useTranslation();
  const router = useRouter();
  const { slug, setSlug, profile, profiles, loading } = useActiveProfile();

  const [open, setOpen] = React.useState(false);
  const triggerRef = React.useRef<HTMLButtonElement | null>(null);
  const panelRef = React.useRef<HTMLDivElement | null>(null);
  const [focusIdx, setFocusIdx] = React.useState<number>(0);

  // Close on outside click / Escape.
  React.useEffect(() => {
    if (!open) return;
    function onDocClick(e: MouseEvent) {
      const t = e.target as Node | null;
      if (
        t &&
        !panelRef.current?.contains(t) &&
        !triggerRef.current?.contains(t)
      ) {
        setOpen(false);
      }
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") {
        setOpen(false);
        triggerRef.current?.focus();
      }
    }
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  // Items: every profile + a synthetic "manage" item at the bottom.
  // Indices 0..(profiles.length-1) → switch slug; the last index is "manage".
  const manageIdx = profiles.length;

  function activate(idx: number) {
    if (idx === manageIdx) {
      setOpen(false);
      router.push("/profiles");
      return;
    }
    const target = profiles[idx];
    if (!target) return;
    setSlug(target.slug);
    setOpen(false);
    triggerRef.current?.focus();
  }

  function onTriggerKey(e: React.KeyboardEvent<HTMLButtonElement>) {
    if (e.key === "ArrowDown" || e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      setOpen(true);
      // Focus the first item next frame so the listbox has rendered.
      requestAnimationFrame(() => {
        setFocusIdx(0);
      });
    }
  }

  function onItemKey(e: React.KeyboardEvent<HTMLButtonElement>, idx: number) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setFocusIdx((idx + 1) % (manageIdx + 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setFocusIdx((idx - 1 + manageIdx + 1) % (manageIdx + 1));
    } else if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      activate(idx);
    }
  }

  // Sync focus with focusIdx whenever the panel is open.
  React.useEffect(() => {
    if (!open) return;
    const el = panelRef.current?.querySelector<HTMLButtonElement>(
      `[data-idx='${focusIdx}']`,
    );
    el?.focus();
  }, [open, focusIdx]);

  const triggerLabel = profile
    ? profile.display_name || profile.slug
    : slug;

  return (
    <div
      className={cn("relative inline-flex", className)}
      data-testid="profile-switcher"
    >
      <button
        ref={triggerRef}
        type="button"
        onClick={() => setOpen((v) => !v)}
        onKeyDown={onTriggerKey}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={t("profiles.switcherSelectAria")}
        title={t("profiles.switcherLabel")}
        data-testid="profile-switcher-trigger"
        className={cn(
          "group flex h-8 items-center gap-1.5 rounded-lg border border-tp-glass-edge bg-tp-glass-inner px-2 text-[12px] text-tp-ink-2 transition-colors",
          "hover:border-tp-glass-edge-strong hover:bg-tp-glass-inner-hover hover:text-tp-ink",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/40",
        )}
      >
        <Users className="h-3.5 w-3.5 shrink-0" aria-hidden />
        <span className="font-mono">{triggerLabel}</span>
        <ChevronDown
          className={cn(
            "h-3 w-3 shrink-0 text-tp-ink-3 transition-transform",
            open && "rotate-180",
          )}
          aria-hidden
        />
      </button>

      {open ? (
        <div
          ref={panelRef}
          role="listbox"
          aria-label={t("profiles.switcherLabel")}
          data-testid="profile-switcher-panel"
          className={cn(
            "absolute right-0 top-[calc(100%+4px)] z-50 min-w-[200px] overflow-hidden rounded-lg border border-tp-glass-edge bg-tp-glass p-1 shadow-tp-panel backdrop-blur-glass",
          )}
        >
          {loading && profiles.length === 0 ? (
            <div className="px-2 py-1.5 text-[11px] text-tp-ink-3">
              {t("profiles.switcherLabel")}…
            </div>
          ) : null}
          {profiles.map((p, idx) => {
            const active = p.slug === slug;
            return (
              <button
                key={p.slug}
                type="button"
                role="option"
                aria-selected={active}
                data-idx={idx}
                data-testid={`profile-switcher-item-${p.slug}`}
                onClick={() => activate(idx)}
                onKeyDown={(e) => onItemKey(e, idx)}
                className={cn(
                  "flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-[12px] transition-colors",
                  "hover:bg-tp-glass-inner",
                  "focus-visible:bg-tp-glass-inner focus-visible:outline-none",
                  active ? "text-tp-ink" : "text-tp-ink-2",
                )}
              >
                <Check
                  className={cn(
                    "h-3 w-3 shrink-0",
                    active ? "opacity-100 text-tp-amber" : "opacity-0",
                  )}
                  aria-hidden
                />
                <span className="flex-1 truncate">
                  <span className="font-mono">{p.slug}</span>
                  {p.display_name && p.display_name !== p.slug ? (
                    <span className="ml-1.5 text-tp-ink-3">
                      {p.display_name}
                    </span>
                  ) : null}
                </span>
              </button>
            );
          })}
          <div
            role="separator"
            aria-orientation="horizontal"
            className="my-1 h-px bg-tp-glass-edge"
          />
          <button
            type="button"
            role="option"
            aria-selected={false}
            data-idx={manageIdx}
            data-testid="profile-switcher-manage"
            onClick={() => activate(manageIdx)}
            onKeyDown={(e) => onItemKey(e, manageIdx)}
            className={cn(
              "flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-[12px] text-tp-ink-2 transition-colors",
              "hover:bg-tp-glass-inner hover:text-tp-ink",
              "focus-visible:bg-tp-glass-inner focus-visible:outline-none",
            )}
          >
            <Settings className="h-3 w-3 shrink-0 opacity-70" aria-hidden />
            <span className="flex-1">{t("profiles.switcherManage")}</span>
          </button>
        </div>
      ) : null}
    </div>
  );
}
