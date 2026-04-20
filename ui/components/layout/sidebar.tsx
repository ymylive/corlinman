"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Activity,
  Bot,
  Boxes,
  ClipboardCheck,
  Database,
  FileTerminal,
  MessageCircle,
  Route,
  Settings,
  Timer,
} from "lucide-react";
import { cn } from "@/lib/utils";

interface NavItem {
  href: string;
  labelZh: string;
  icon: React.ComponentType<{ className?: string }>;
}

// Keep this list aligned with plan §4 (the admin route list).
const ITEMS: NavItem[] = [
  { href: "/", labelZh: "概览", icon: Activity },
  { href: "/plugins", labelZh: "插件", icon: Boxes },
  { href: "/agents", labelZh: "Agent", icon: Bot },
  { href: "/rag", labelZh: "RAG", icon: Database },
  { href: "/channels/qq", labelZh: "QQ 通道", icon: MessageCircle },
  { href: "/scheduler", labelZh: "定时任务", icon: Timer },
  { href: "/approvals", labelZh: "工具审批", icon: ClipboardCheck },
  { href: "/logs", labelZh: "日志", icon: FileTerminal },
  { href: "/config", labelZh: "配置", icon: Settings },
  { href: "/models", labelZh: "模型路由", icon: Route },
];

export function Sidebar() {
  const pathname = usePathname();
  return (
    <aside className="flex w-56 shrink-0 flex-col border-r border-border bg-muted/20 px-3 py-4">
      <nav className="flex flex-col gap-1" aria-label="admin">
        {ITEMS.map((item) => {
          const active =
            item.href === "/"
              ? pathname === "/"
              : pathname === item.href || pathname.startsWith(`${item.href}/`);
          const Icon = item.icon;
          return (
            <Link
              key={item.href}
              href={item.href as never}
              className={cn(
                "flex items-center gap-2 rounded-md px-3 py-2 text-sm transition-colors",
                active
                  ? "bg-accent text-accent-foreground"
                  : "text-muted-foreground hover:bg-accent/50 hover:text-foreground",
              )}
            >
              <Icon className="h-4 w-4" />
              <span>{item.labelZh}</span>
            </Link>
          );
        })}
      </nav>
    </aside>
  );
}
