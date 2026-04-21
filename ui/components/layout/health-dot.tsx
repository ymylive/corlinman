"use client";

import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { cn } from "@/lib/utils";
import { fetchHealth, type HealthStatus } from "@/lib/api";

/**
 * Tiny colored dot in the topnav tied to GET /health.
 *
 *   ok   → green  (all checks pass)
 *   warn → amber  (degraded — some checks fail but gateway up)
 *   err  → red    (gateway unreachable / 503)
 *
 * Refreshed every 30s per the brief.
 */
export function HealthDot({ className }: { className?: string }) {
  const { t } = useTranslation();
  const q = useQuery<HealthStatus>({
    queryKey: ["admin", "health"],
    queryFn: fetchHealth,
    refetchInterval: 30_000,
    retry: false,
  });

  let tone: "ok" | "warn" | "err" = "warn";
  let title = t("health.checking");
  if (q.isError) {
    tone = "err";
    title = t("health.gatewayUnreachable");
  } else if (q.data) {
    const status = q.data.status ?? "unknown";
    if (status === "ok" || status === "healthy") {
      tone = "ok";
      const suffix = q.data.checks
        ? t("health.ariaCheckCount", { n: q.data.checks.length })
        : "";
      title = `${t("health.healthy")}${suffix}`;
    } else if (status === "degraded" || status === "warn") {
      tone = "warn";
      title = t("health.degraded");
    } else {
      tone = "err";
      title = `${t("health.offline")} (${status})`;
    }
  }

  return (
    <span
      title={title}
      aria-label={t("health.gatewayHealth", { label: title })}
      className={cn("inline-flex items-center gap-1.5 text-xs", className)}
    >
      <span
        className={cn(
          "inline-block h-2 w-2 rounded-full",
          tone === "ok" && "bg-ok",
          tone === "warn" && "bg-warn",
          tone === "err" && "bg-err",
        )}
      />
      <span className="text-muted-foreground">
        {tone === "ok"
          ? t("health.okShort")
          : tone === "warn"
            ? t("health.warnShort")
            : t("health.errShort")}
      </span>
    </span>
  );
}
