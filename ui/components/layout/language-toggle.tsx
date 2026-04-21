"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import { cn } from "@/lib/utils";

/**
 * Small pill in the topnav that flips between `zh-CN` and `en`. The choice
 * persists via the i18next LanguageDetector `caches: ["localStorage"]` hook
 * (see lib/i18n.ts).
 */
export function LanguageToggle({ className }: { className?: string }) {
  const { i18n, t } = useTranslation();
  const [mounted, setMounted] = React.useState(false);
  React.useEffect(() => setMounted(true), []);

  const current = i18n.language?.startsWith("zh") ? "zh-CN" : "en";
  const next = current === "zh-CN" ? "en" : "zh-CN";
  const label = current === "zh-CN" ? "中" : "EN";
  const aria =
    next === "zh-CN"
      ? t("nav.switchToChinese")
      : t("nav.switchToEnglish");

  return (
    <button
      type="button"
      aria-label={mounted ? aria : t("nav.switchLanguage")}
      onClick={() => i18n.changeLanguage(next)}
      className={cn(
        "inline-flex h-8 min-w-[32px] items-center justify-center rounded-md border border-transparent px-1.5 font-mono text-[11px] font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-foreground",
        className,
      )}
    >
      {mounted ? label : "中"}
    </button>
  );
}
