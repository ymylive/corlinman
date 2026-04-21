"use client";

import * as React from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ThemeProvider } from "next-themes";
import { Toaster } from "sonner";
import {
  DEFAULT_LOCALE,
  type Locale,
  type MessageKey,
  translate,
} from "@/lib/i18n";
import { CommandPaletteProvider } from "./cmdk-palette";

// --- i18n context (subset of next-intl API, see lib/i18n/index.ts) ----------

interface I18nCtx {
  locale: Locale;
  setLocale: (l: Locale) => void;
  t: (key: MessageKey) => string;
}

const I18nContext = React.createContext<I18nCtx | null>(null);

export function useI18n(): I18nCtx {
  const ctx = React.useContext(I18nContext);
  if (!ctx) throw new Error("useI18n must be used inside <Providers />");
  return ctx;
}

// --- providers --------------------------------------------------------------

interface ProvidersProps {
  children: React.ReactNode;
}

export function Providers({ children }: ProvidersProps) {
  const [locale, setLocale] = React.useState<Locale>(DEFAULT_LOCALE);
  const [queryClient] = React.useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: { staleTime: 30_000, refetchOnWindowFocus: false },
        },
      }),
  );

  const i18nValue = React.useMemo<I18nCtx>(
    () => ({
      locale,
      setLocale,
      t: (key) => translate(locale, key),
    }),
    [locale],
  );

  return (
    <ThemeProvider
      attribute="class"
      defaultTheme="dark"
      enableSystem={false}
      disableTransitionOnChange
    >
      <QueryClientProvider client={queryClient}>
        <I18nContext.Provider value={i18nValue}>
          <CommandPaletteProvider>
            {children}
            <Toaster
              theme="dark"
              position="top-right"
              toastOptions={{
                classNames: {
                  toast:
                    "!border !border-border !bg-popover !text-popover-foreground !font-sans",
                  title: "!text-sm !font-medium",
                  description: "!text-xs !text-muted-foreground",
                },
              }}
              closeButton
              duration={3000}
            />
          </CommandPaletteProvider>
        </I18nContext.Provider>
      </QueryClientProvider>
    </ThemeProvider>
  );
}
