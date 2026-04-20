/**
 * Tiny i18n runtime. We read zh.json/en.json at build time and expose a
 * `useT()` hook that resolves keys against the active locale stored in a
 * React context (see components/providers.tsx).
 *
 * TODO(M6): replace with full next-intl integration once the message
 *           catalog stabilises. The current shape is deliberately a
 *           subset of next-intl's API so the migration is mechanical.
 */

import zh from "./zh.json";
import en from "./en.json";

export const MESSAGES = { zh, en } as const;
export type Locale = keyof typeof MESSAGES;
export const DEFAULT_LOCALE: Locale = "zh";

export type MessageKey = keyof typeof zh;

export function translate(locale: Locale, key: MessageKey): string {
  const table = MESSAGES[locale] as Record<string, string>;
  return table[key] ?? MESSAGES[DEFAULT_LOCALE][key] ?? key;
}
