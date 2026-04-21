"use client";

import * as React from "react";
import { useQuery } from "@tanstack/react-query";
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
  const q = useQuery<HealthStatus>({
    queryKey: ["admin", "health"],
    queryFn: fetchHealth,
    refetchInterval: 30_000,
    retry: false,
  });

  let tone: "ok" | "warn" | "err" = "warn";
  let title = "Checking…";
  if (q.isError) {
    tone = "err";
    title = "Gateway unreachable";
  } else if (q.data) {
    const status = q.data.status ?? "unknown";
    if (status === "ok" || status === "healthy") {
      tone = "ok";
      title = `Healthy${q.data.checks ? ` · ${q.data.checks.length} checks` : ""}`;
    } else if (status === "degraded" || status === "warn") {
      tone = "warn";
      title = "Degraded";
    } else {
      tone = "err";
      title = `Unhealthy (${status})`;
    }
  }

  return (
    <span
      title={title}
      aria-label={`gateway health: ${title}`}
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
        {tone === "ok" ? "healthy" : tone === "warn" ? "degraded" : "offline"}
      </span>
    </span>
  );
}
