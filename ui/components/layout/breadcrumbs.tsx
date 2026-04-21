"use client";

import * as React from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { ChevronRight } from "lucide-react";

const LABELS: Record<string, string> = {
  "": "Dashboard",
  plugins: "Plugins",
  detail: "Detail",
  agents: "Agents",
  rag: "RAG",
  channels: "Channels",
  qq: "QQ",
  scheduler: "Scheduler",
  approvals: "Approvals",
  models: "Models",
  config: "Config",
  logs: "Logs",
};

/** Auto-derived breadcrumb from `usePathname`. */
export function Breadcrumbs() {
  const pathname = usePathname() ?? "/";
  const segments = pathname.split("/").filter(Boolean);

  if (segments.length === 0) {
    return (
      <span className="text-sm font-medium text-foreground">Dashboard</span>
    );
  }

  const crumbs: { href: string; label: string }[] = [];
  let acc = "";
  for (const seg of segments) {
    acc += `/${seg}`;
    crumbs.push({ href: acc, label: LABELS[seg] ?? seg });
  }

  return (
    <nav aria-label="breadcrumb" className="flex items-center gap-1 text-sm">
      <Link
        href="/"
        className="text-muted-foreground transition-colors hover:text-foreground"
      >
        Dashboard
      </Link>
      {crumbs.map((c, i) => (
        <React.Fragment key={c.href}>
          <ChevronRight className="h-3.5 w-3.5 text-muted-foreground/60" />
          {i === crumbs.length - 1 ? (
            <span className="font-medium text-foreground">{c.label}</span>
          ) : (
            <Link
              href={c.href as never}
              className="text-muted-foreground transition-colors hover:text-foreground"
            >
              {c.label}
            </Link>
          )}
        </React.Fragment>
      ))}
    </nav>
  );
}
