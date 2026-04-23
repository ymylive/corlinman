"use client";

import * as React from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ThemeProvider } from "next-themes";
import { Toaster } from "sonner";
import { I18nextProvider } from "react-i18next";

import { i18next, initI18n } from "@/lib/i18n";
import { CommandPaletteProvider } from "./cmdk-palette";

// Init at module load. `initI18n()` is SSR-safe: on the server it skips
// the LanguageDetector plugin and defaults to zh-CN, matching the
// `<html lang="zh-CN">` we emit. On the client it re-runs inside
// <Providers /> too but the function is idempotent.
initI18n();

// --- providers --------------------------------------------------------------

interface ProvidersProps {
  children: React.ReactNode;
}

export function Providers({ children }: ProvidersProps) {
  const [queryClient] = React.useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: { staleTime: 30_000, refetchOnWindowFocus: false },
        },
      }),
  );

  // Safety net for SSR/test paths where the module-scope init didn't run.
  React.useEffect(() => {
    initI18n();
  }, []);

  return (
    <ThemeProvider
      // Dual-write the theme onto both `.dark` class (for Tailwind dark:
      // variants still used by legacy pages) and `data-theme` attribute
      // (Tidepool scope selector). Using the Tidepool storage key so the
      // inline boot script in app/layout.tsx and next-themes agree on
      // their source of truth — no FOUC and no race.
      attribute={["class", "data-theme"]}
      defaultTheme="dark"
      enableSystem={false}
      disableTransitionOnChange
      storageKey="corlinman-theme"
    >
      <QueryClientProvider client={queryClient}>
        <I18nextProvider i18n={i18next}>
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
        </I18nextProvider>
      </QueryClientProvider>
    </ThemeProvider>
  );
}
