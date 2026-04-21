"use client";

import * as React from "react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTheme } from "next-themes";
import { useTranslation } from "react-i18next";
import { ArrowLeft, Circle } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import { fetchAgent, saveAgent, type AgentContent } from "@/lib/api";

const Editor = dynamic(() => import("@monaco-editor/react"), { ssr: false });

/**
 * Agent detail page. Monaco edits the full file body. Name is passed as
 * `?name=…` (not a dynamic segment — keeps Next static export happy).
 *
 * Dirty indicator: the small filled dot next to the filename flips on when
 * the draft diverges from the last loaded content.
 */
export default function AgentDetailPage() {
  const { t } = useTranslation();
  const search = useSearchParams();
  const name = search?.get("name") ?? "";
  const qc = useQueryClient();
  const { resolvedTheme } = useTheme();

  const agent = useQuery<AgentContent>({
    queryKey: ["admin", "agents", name],
    queryFn: () => fetchAgent(name),
    enabled: !!name,
  });

  const [draft, setDraft] = React.useState("");
  const [baseline, setBaseline] = React.useState("");
  const [initialized, setInitialized] = React.useState(false);
  React.useEffect(() => {
    if (agent.data && !initialized) {
      setDraft(agent.data.content);
      setBaseline(agent.data.content);
      setInitialized(true);
    }
  }, [agent.data, initialized]);

  const dirty = initialized && draft !== baseline;

  const save = useMutation({
    mutationFn: () => saveAgent(name, draft),
    onSuccess: () => {
      setBaseline(draft);
      qc.invalidateQueries({ queryKey: ["admin", "agents", name] });
      qc.invalidateQueries({ queryKey: ["admin", "agents"] });
    },
  });

  if (!name) {
    return (
      <p className="text-sm text-muted-foreground">
        {t("agents.missingName")}{" "}
        <Link href="/agents" className="underline">
          {t("agents.agentListLink")}
        </Link>
      </p>
    );
  }

  return (
    <div className="flex flex-1 flex-col space-y-4">
      <header className="flex items-start justify-between gap-4">
        <div className="space-y-1">
          <Link
            href="/agents"
            className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
          >
            <ArrowLeft className="h-3 w-3" />
            {t("agents.backToList")}
          </Link>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
            {name}
            {dirty ? (
              <Circle
                className="h-2 w-2 fill-warn text-warn"
                aria-label={t("agents.unsavedIndicator")}
              />
            ) : null}
          </h1>
          {agent.data ? (
            <p className="font-mono text-xs text-muted-foreground">
              {agent.data.file_path} · {agent.data.bytes} bytes
              {agent.data.last_modified ? ` · ${agent.data.last_modified}` : ""}
            </p>
          ) : null}
        </div>
        <Button
          size="sm"
          onClick={() => save.mutate()}
          disabled={!initialized || save.isPending}
          data-testid="agent-save-btn"
        >
          {save.isPending ? t("common.saving") : t("common.save")}
        </Button>
      </header>

      {agent.isPending ? (
        <Skeleton className="h-[600px] w-full" />
      ) : agent.isError ? (
        <p className="text-sm text-destructive">
          {t("agents.loadFailed")}: {(agent.error as Error).message}
        </p>
      ) : (
        <section className="flex-1 overflow-hidden rounded-lg border border-border bg-panel">
          <Editor
            height="600px"
            defaultLanguage="markdown"
            value={draft}
            onChange={(v) => setDraft(v ?? "")}
            theme={resolvedTheme === "light" ? "vs-light" : "vs-dark"}
            options={{
              fontSize: 13,
              minimap: { enabled: false },
              wordWrap: "on",
              scrollBeyondLastLine: false,
              fontFamily: "var(--font-geist-mono), ui-monospace, monospace",
            }}
          />
        </section>
      )}

      {save.isError ? (
        <p className="text-sm text-destructive">
          {t("agents.saveFailed")}: {(save.error as Error).message}
        </p>
      ) : save.isSuccess ? (
        <p
          className={cn(
            "text-sm text-ok",
            // Pulse the success text briefly
            "animate-in fade-in-0",
          )}
        >
          {t("agents.saveSuccess")}
        </p>
      ) : null}
    </div>
  );
}
