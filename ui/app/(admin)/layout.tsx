import { TopNav } from "@/components/layout/nav";
import { Sidebar } from "@/components/layout/sidebar";

/**
 * Admin route group layout — shared TopNav + Sidebar across every
 * /plugins, /agents, /rag, /channels/qq, /scheduler, /approvals, /logs,
 * /config, /models page (plan §4).
 */
export default function AdminLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <div className="flex min-h-dvh flex-col">
      <TopNav />
      <div className="flex flex-1">
        <Sidebar />
        <main className="flex-1 space-y-6 p-6">{children}</main>
      </div>
    </div>
  );
}
