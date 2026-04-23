"use client";

import * as React from "react";
import { Sun, Moon } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * Sun/moon pill toggle. Reads and writes `document.documentElement.dataset.theme`
 * (`"light"` | `"dark"`) and persists via `localStorage[STORAGE_KEY]`.
 *
 * This is a **controlled-via-DOM** component: we don't hold state in React.
 * Doing so lets an inline boot script in `app/layout.tsx` set the attribute
 * before React hydrates, avoiding a day/night flash on reload.
 *
 * Accessibility:
 *   - Each option is a real <button> with `aria-pressed` reflecting state.
 *   - The focused option is visually distinguishable in both themes.
 *
 * Phase 1 lands the component. Phase 2 will mount a single instance in the
 * TopNav and add the boot-script hydration.
 */

// Storage key + attribute keep in sync with the <ThemeProvider> config in
// components/providers.tsx (which uses attribute=["class","data-theme"]
// and storageKey=STORAGE_KEY). Exporting so the inline boot script can
// pick up the same constant by reading the globals file at build time.
const STORAGE_KEY = "corlinman-theme";

export type Theme = "light" | "dark";

function currentTheme(): Theme {
  if (typeof document === "undefined") return "dark";
  const el = document.documentElement;
  return el.dataset.theme === "light" ? "light" : "dark";
}

function applyTheme(next: Theme) {
  if (typeof document === "undefined") return;
  document.documentElement.dataset.theme = next;
  // `.dark` class remains in sync for Tailwind dark: utility compatibility.
  if (next === "dark") document.documentElement.classList.add("dark");
  else document.documentElement.classList.remove("dark");
  try {
    window.localStorage.setItem(STORAGE_KEY, next);
  } catch {
    // Safari private mode etc — non-fatal.
  }
}

export interface ThemeToggleProps
  extends Omit<React.HTMLAttributes<HTMLDivElement>, "onChange"> {
  /** Initial theme. Defaults to reading the DOM (set by boot script). */
  initial?: Theme;
  /** Fires *after* the DOM + localStorage write is done. */
  onThemeChange?: (next: Theme) => void;
}

export const ThemeToggle = React.forwardRef<HTMLDivElement, ThemeToggleProps>(
  function ThemeToggle({ initial, onThemeChange, className, ...rest }, ref) {
    const [theme, setTheme] = React.useState<Theme>(() => initial ?? currentTheme());

    // Keep in sync if another component / extension changes the attribute.
    React.useEffect(() => {
      const el = document.documentElement;
      const observer = new MutationObserver(() => {
        const next = currentTheme();
        setTheme((prev) => (prev === next ? prev : next));
      });
      observer.observe(el, { attributes: true, attributeFilter: ["data-theme"] });
      return () => observer.disconnect();
    }, []);

    const pick = React.useCallback(
      (next: Theme) => {
        applyTheme(next);
        setTheme(next);
        onThemeChange?.(next);
      },
      [onThemeChange],
    );

    return (
      <div
        ref={ref}
        role="tablist"
        aria-label="Theme"
        className={cn(
          "inline-flex gap-0.5 p-0.5",
          "bg-tp-glass-inner border border-tp-glass-edge rounded-full",
          className,
        )}
        {...rest}
      >
        <Option
          active={theme === "light"}
          label="Day mode"
          onClick={() => pick("light")}
          icon={<Sun className="h-3.5 w-3.5" />}
          mode="light"
        />
        <Option
          active={theme === "dark"}
          label="Night mode"
          onClick={() => pick("dark")}
          icon={<Moon className="h-3.5 w-3.5" />}
          mode="dark"
        />
      </div>
    );
  },
);

function Option({
  active,
  label,
  icon,
  onClick,
  mode,
}: {
  active: boolean;
  label: string;
  icon: React.ReactNode;
  onClick: () => void;
  mode: Theme;
}) {
  return (
    <button
      type="button"
      role="tab"
      aria-pressed={active}
      aria-label={label}
      data-mode={mode}
      onClick={onClick}
      className={cn(
        "flex h-6 w-6 items-center justify-center rounded-full",
        "text-tp-ink-3 transition-colors",
        "hover:text-tp-ink-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/60",
        active &&
          "bg-tp-glass-inner-hover text-tp-amber shadow-[0_1px_2px_rgba(0,0,0,0.1),inset_0_0_0_1px_var(--tp-glass-edge)]",
      )}
    >
      {icon}
    </button>
  );
}

export { STORAGE_KEY as THEME_STORAGE_KEY };
export default ThemeToggle;
