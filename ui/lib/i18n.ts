/**
 * i18next bootstrap for the admin UI.
 *
 * Two locales: `zh-CN` (default, authoritative) and `en`. The detector
 * checks localStorage (`corlinman_lang`) first, then `navigator.language`
 * (anything starting with `zh` → `zh-CN`, otherwise `en`).
 *
 * Two init paths:
 *   - Client (has `window`): plug `LanguageDetector` so user choice
 *     persists across visits.
 *   - Server (static export / SSG): init without the detector, forced to
 *     `zh-CN` so the server-rendered HTML matches the default language.
 */

import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import LanguageDetector from "i18next-browser-languagedetector";

import { zhCN } from "./locales/zh-CN";
import { en } from "./locales/en";

export const LANG_STORAGE_KEY = "corlinman_lang";
export const SUPPORTED_LANGS = ["zh-CN", "en"] as const;
export type SupportedLang = (typeof SUPPORTED_LANGS)[number];
export const DEFAULT_LANG: SupportedLang = "zh-CN";

/**
 * Resolve the initial language synchronously from localStorage /
 * navigator.language. Used by the inline boot script and by initI18n so
 * both agree on the starting locale (no FOUC).
 */
export function resolveInitialLang(): SupportedLang {
  if (typeof window === "undefined") return DEFAULT_LANG;
  try {
    const stored = window.localStorage.getItem(LANG_STORAGE_KEY);
    if (stored === "zh-CN" || stored === "en") return stored;
  } catch {
    /* fall through */
  }
  const nav = (
    typeof navigator !== "undefined" ? navigator.language : ""
  ).toLowerCase();
  return nav.startsWith("zh") ? "zh-CN" : "en";
}

let initialized = false;

/** Initialise i18next once. Safe to call more than once. */
export function initI18n(): typeof i18next {
  if (initialized) return i18next;
  initialized = true;

  const isClient = typeof window !== "undefined";
  const initialLang = resolveInitialLang();

  const builder = isClient
    ? i18next.use(LanguageDetector).use(initReactI18next)
    : i18next.use(initReactI18next);

  builder.init({
    resources: {
      "zh-CN": { translation: zhCN },
      en: { translation: en },
    },
    lng: initialLang,
    fallbackLng: DEFAULT_LANG,
    supportedLngs: SUPPORTED_LANGS as readonly string[] as string[],
    interpolation: { escapeValue: false },
    detection: isClient
      ? {
          order: ["localStorage", "navigator"],
          lookupLocalStorage: LANG_STORAGE_KEY,
          caches: ["localStorage"],
        }
      : undefined,
    returnNull: false,
    // Synchronous init — we bundle resources inline, there's nothing
    // async to wait for. Matters for vitest/SSG: React tests / static
    // export render before an async init promise would resolve. (v26
    // renamed `initImmediate` → `initAsync`, inverted semantics.)
    initAsync: false,
    react: { useSuspense: false },
  });

  // Keep <html lang> in sync so screen readers / search engines see it.
  if (typeof document !== "undefined") {
    document.documentElement.setAttribute("lang", initialLang);
    i18next.on("languageChanged", (lng) => {
      document.documentElement.setAttribute("lang", lng);
      try {
        window.localStorage.setItem(LANG_STORAGE_KEY, lng);
      } catch {
        /* storage disabled */
      }
    });
  }

  return i18next;
}

export { i18next };
