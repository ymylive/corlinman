"use client";

/**
 * /admin/newapi — newapi connector page.
 *
 * Three sections:
 *   1. Connection summary card  (GET /admin/newapi)
 *   2. Live channel table       (GET /admin/newapi/channels?type=llm)
 *   3. Round-trip test button   (POST /admin/newapi/test)
 *
 * 503 from /admin/newapi means no enabled `kind = "newapi"` provider
 * is configured. The page surfaces a `BackendPendingBanner` directing
 * the operator to /onboard.
 */

import * as React from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

import {
  CorlinmanApiError,
  fetchNewapiChannels,
  fetchNewapiSummary,
  testNewapi,
  type NewapiChannel,
  type NewapiSummary,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";

export default function NewapiPage() {
  const { t } = useTranslation();
  const summary = useQuery<NewapiSummary>({
    queryKey: ["admin", "newapi"],
    queryFn: fetchNewapiSummary,
    retry: false,
  });

  if (summary.error) {
    const err = summary.error as CorlinmanApiError;
    if (err.status === 503) return <BackendPendingBanner />;
    return (
      <div className="rounded-md border border-destructive p-4 text-destructive">
        {err.message}
      </div>
    );
  }
  if (summary.isLoading || !summary.data) {
    return <Skeleton className="h-32 w-full" />;
  }

  return (
    <div className="space-y-6">
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">
          {t("admin.newapi.title", "newapi 连接")}
        </h1>
        <p className="text-sm text-tp-ink-3">
          {t(
            "admin.newapi.subtitle",
            "QuantumNous/new-api 频道池：LLM、嵌入与 TTS 模型统一通过这里转发。",
          )}
        </p>
      </header>
      <ConnectionCard summary={summary.data} />
      <ChannelsSection />
    </div>
  );
}

function BackendPendingBanner() {
  const { t } = useTranslation();
  return (
    <div className="rounded-md border bg-tp-glass-inner p-4">
      <h2 className="text-lg font-semibold">
        {t("admin.newapi.notConfiguredTitle", "未配置 newapi")}
      </h2>
      <p className="mt-1 text-sm text-tp-ink-3">
        {t(
          "admin.newapi.notConfiguredBody",
          "尚未启用 `kind = \"newapi\"` 的 provider。请到 /onboard 走一次首次配置向导，或手动在 config.toml 里添加。",
        )}
      </p>
    </div>
  );
}

function ConnectionCard({ summary }: { summary: NewapiSummary }) {
  const { t } = useTranslation();
  const testMut = useMutation({
    mutationFn: (model: string) => testNewapi(model),
    onSuccess: (r) =>
      toast.success(
        t("admin.newapi.testOk", {
          ms: r.latency_ms,
          status: r.status,
          defaultValue: "{{ms}} ms (HTTP {{status}})",
        }),
      ),
    onError: (e: CorlinmanApiError) =>
      toast.error(e.message ?? "newapi_test_failed"),
  });

  const [testModel, setTestModel] = React.useState("gpt-4o-mini");

  return (
    <section className="rounded-md border bg-tp-glass-inner p-4">
      <h2 className="mb-3 font-medium">
        {t("admin.newapi.connection", "连接信息")}
      </h2>
      <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm">
        <dt className="text-tp-ink-3">{t("admin.newapi.baseUrl", "地址")}</dt>
        <dd className="font-mono">{summary.connection.base_url}</dd>
        <dt className="text-tp-ink-3">{t("admin.newapi.token", "用户令牌")}</dt>
        <dd className="font-mono">{summary.connection.token_masked}</dd>
        <dt className="text-tp-ink-3">
          {t("admin.newapi.adminKey", "系统令牌")}
        </dt>
        <dd>
          {summary.connection.admin_key_present
            ? t("common.yes", "已配置")
            : t("common.no", "未配置")}
        </dd>
        <dt className="text-tp-ink-3">{t("admin.newapi.enabled", "启用")}</dt>
        <dd>
          {summary.connection.enabled
            ? t("common.yes", "是")
            : t("common.no", "否")}
        </dd>
      </dl>
      <div className="mt-3 flex items-end gap-2">
        <div className="flex-1">
          <label
            htmlFor="test-model"
            className="text-xs text-tp-ink-3"
          >
            {t("admin.newapi.testModel", "测试模型 ID")}
          </label>
          <input
            id="test-model"
            className="mt-1 w-full rounded-md border bg-background px-2 py-1.5 text-sm font-mono"
            value={testModel}
            onChange={(e) => setTestModel(e.target.value)}
          />
        </div>
        <Button
          type="button"
          onClick={() => testMut.mutate(testModel)}
          disabled={testMut.isPending}
        >
          {testMut.isPending
            ? t("common.submitting", "测试中…")
            : t("admin.newapi.testButton", "测试连接")}
        </Button>
      </div>
    </section>
  );
}

function ChannelsSection() {
  const { t } = useTranslation();
  const [type, setType] = React.useState<"llm" | "embedding" | "tts">("llm");
  const channels = useQuery<{ channels: NewapiChannel[] }>({
    queryKey: ["admin", "newapi", "channels", type],
    queryFn: () => fetchNewapiChannels(type),
    retry: false,
  });

  return (
    <section className="rounded-md border bg-tp-glass-inner p-4">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="font-medium">{t("admin.newapi.channels", "频道列表")}</h2>
        <div className="flex gap-1 text-xs">
          {(["llm", "embedding", "tts"] as const).map((k) => (
            <button
              key={k}
              type="button"
              onClick={() => setType(k)}
              className={`rounded-md border px-2 py-1 ${
                type === k
                  ? "border-primary bg-primary text-primary-foreground"
                  : "border-tp-glass-edge text-tp-ink-3"
              }`}
            >
              {t(`admin.newapi.channelType.${k}`, k)}
            </button>
          ))}
        </div>
      </div>
      {channels.isLoading ? (
        <Skeleton className="h-24 w-full" />
      ) : channels.error ? (
        <p className="text-sm text-destructive">
          {(channels.error as CorlinmanApiError).message}
        </p>
      ) : channels.data && channels.data.channels.length === 0 ? (
        <p className="text-sm text-tp-ink-3">
          {t("admin.newapi.channelsEmpty", "没有可用的频道。")}
        </p>
      ) : (
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs text-tp-ink-3">
              <th className="py-1 pr-3">ID</th>
              <th className="py-1 pr-3">{t("admin.newapi.channelName", "名称")}</th>
              <th className="py-1 pr-3">{t("admin.newapi.channelModels", "模型")}</th>
              <th className="py-1 pr-3">{t("admin.newapi.channelStatus", "状态")}</th>
            </tr>
          </thead>
          <tbody>
            {channels.data?.channels.map((c) => (
              <tr key={c.id} className="border-t border-tp-glass-edge">
                <td className="py-1 pr-3 font-mono">{c.id}</td>
                <td className="py-1 pr-3">{c.name}</td>
                <td className="py-1 pr-3 font-mono text-xs">{c.models}</td>
                <td className="py-1 pr-3">
                  {c.status === 1 ? "✓" : c.status === 2 ? "⏸" : "✗"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
