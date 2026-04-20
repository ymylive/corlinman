import { TopNav } from "@/components/layout/nav";
import { Sidebar } from "@/components/layout/sidebar";
import { TodoCard } from "@/components/todo-card";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

/**
 * Root overview dashboard (plan §4 app/page.tsx). At M0 we render static
 * placeholder cards; M6 wires the real /admin/metrics + /admin/health feed.
 */
export default function HomePage() {
  return (
    <div className="flex min-h-dvh flex-col">
      <TopNav />
      <div className="flex flex-1">
        <Sidebar />
        <main className="flex-1 space-y-6 p-6">
          <header className="space-y-1">
            <h1 className="text-2xl font-semibold tracking-tight">概览</h1>
            <p className="text-sm text-muted-foreground">
              Rust 网关 / Python AI 层 / 通道健康状态一览。占位面板，M6 接入。
            </p>
          </header>

          <section className="grid gap-4 md:grid-cols-3">
            <Card>
              <CardHeader>
                <CardTitle>请求速率</CardTitle>
                <CardDescription>最近 5 分钟</CardDescription>
              </CardHeader>
              <CardContent>
                <div className="text-3xl font-mono">— req/s</div>
                <p className="text-xs text-muted-foreground">
                  TODO(M6): 订阅 /admin/metrics SSE
                </p>
              </CardContent>
            </Card>
            <Card>
              <CardHeader>
                <CardTitle>活跃 Session</CardTitle>
                <CardDescription>by ChannelBinding</CardDescription>
              </CardHeader>
              <CardContent>
                <div className="text-3xl font-mono">—</div>
                <p className="text-xs text-muted-foreground">
                  TODO(M6): /admin/sessions
                </p>
              </CardContent>
            </Card>
            <Card>
              <CardHeader>
                <CardTitle>插件健康</CardTitle>
                <CardDescription>enabled / failing</CardDescription>
              </CardHeader>
              <CardContent>
                <div className="text-3xl font-mono">—</div>
                <p className="text-xs text-muted-foreground">
                  TODO(M6): /admin/plugins?health=1
                </p>
              </CardContent>
            </Card>
          </section>

          <TodoCard
            title="下一步"
            milestone="M6"
            description="当 gateway /admin/* 契约冻结后，此页切实时数据流。"
            todos={[
              "对接 /admin/metrics (Prometheus scrape JSON)",
              "订阅 /admin/events SSE (session 创建/销毁)",
              "嵌入 TraceIdBadge，点击跳转 /logs?trace=…",
            ]}
          />
        </main>
      </div>
    </div>
  );
}
