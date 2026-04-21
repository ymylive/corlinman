"use client";

import * as React from "react";
import dynamic from "next/dynamic";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTheme } from "next-themes";
import { useTranslation } from "react-i18next";
import { AnimatePresence, motion } from "framer-motion";
import { AlertTriangle, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import {
  fetchConfig,
  fetchConfigSchema,
  postConfig,
  type ConfigGetResponse,
  type ConfigPostResponse,
} from "@/lib/api";

// Monaco isn't SSR-safe; lazy-load on the client only.
const Editor = dynamic(() => import("@monaco-editor/react"), { ssr: false });

/**
 * Config editor. Left rail = section navigation. Right = Monaco in INI/TOML
 * mode. Validate does a dry-run POST; Save persists + ArcSwap hot-reload.
 * Issues panel slides up from the bottom when a validation returns issues.
 *
 * Preserved E2E contracts:
 *   - `config-save-btn` testid.
 *   - "new version: <sha>" text on successful save (scoped to the result).
 */
const SECTION_HEADERS = [
  "server",
  "admin",
  "providers",
  "models",
  "channels",
  "rag",
  "approvals",
  "scheduler",
  "logging",
  "meta",
];

export default function ConfigPage() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const { resolvedTheme } = useTheme();
  const config = useQuery<ConfigGetResponse>({
    queryKey: ["admin", "config"],
    queryFn: fetchConfig,
  });
  const schema = useQuery({
    queryKey: ["admin", "config", "schema"],
    queryFn: fetchConfigSchema,
    staleTime: Infinity,
  });
  React.useEffect(() => {
    if (schema.data && typeof window !== "undefined") {
      (window as unknown as Record<string, unknown>).__corlinmanConfigSchema =
        schema.data;
    }
  }, [schema.data]);

  const [draft, setDraft] = React.useState<string>("");
  const [initialized, setInitialized] = React.useState(false);
  const [activeSection, setActiveSection] = React.useState<string>("server");
  const [validateResult, setValidateResult] =
    React.useState<ConfigPostResponse | null>(null);
  const [saveResult, setSaveResult] =
    React.useState<ConfigPostResponse | null>(null);
  const [issuesOpen, setIssuesOpen] = React.useState(false);

  React.useEffect(() => {
    if (config.data && !initialized) {
      setDraft(config.data.toml);
      setInitialized(true);
    }
  }, [config.data, initialized]);

  const validateMutation = useMutation({
    mutationFn: () => postConfig(draft, true),
    onSuccess: (r) => {
      setValidateResult(r);
      setSaveResult(null);
      if (r.issues.length > 0) setIssuesOpen(true);
    },
    onError: () => setValidateResult(null),
  });
  const saveMutation = useMutation({
    mutationFn: () => postConfig(draft, false),
    onSuccess: (r) => {
      setSaveResult(r);
      setValidateResult(null);
      if (r.issues.length > 0) setIssuesOpen(true);
      qc.invalidateQueries({ queryKey: ["admin", "config"] });
    },
    onError: () => setSaveResult(null),
  });

  // Monaco editor handle for section jumps.
  const editorRef = React.useRef<unknown>(null);
  const onMount = (editor: unknown) => {
    editorRef.current = editor;
  };
  const jumpToSection = (section: string) => {
    setActiveSection(section);
    const ed = editorRef.current as
      | {
          revealLineInCenter?: (n: number) => void;
          setPosition?: (p: { lineNumber: number; column: number }) => void;
        }
      | null;
    if (!ed) return;
    const lines = draft.split("\n");
    const marker = `[${section}]`;
    const markerTable = `[${section}.`;
    const markerArray = `[[${section}.`;
    for (let i = 0; i < lines.length; i++) {
      const l = lines[i]!.trimStart();
      if (
        l.startsWith(marker) ||
        l.startsWith(markerTable) ||
        l.startsWith(markerArray)
      ) {
        ed.revealLineInCenter?.(i + 1);
        ed.setPosition?.({ lineNumber: i + 1, column: 1 });
        break;
      }
    }
  };

  const latestResult = saveResult ?? validateResult;

  return (
    <>
      <header className="flex items-start justify-between gap-4">
        <div className="space-y-1">
          <h1 className="text-2xl font-semibold tracking-tight">
            {t("config.title")}
          </h1>
          <p className="text-sm text-muted-foreground">
            {t("config.subtitle")}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {config.data ? (
            <code className="rounded bg-muted px-2 py-1 font-mono text-[11px]">
              {t("config.version", { v: config.data.version })}
            </code>
          ) : null}
          <Button
            size="sm"
            variant="outline"
            onClick={() => validateMutation.mutate()}
            disabled={!initialized || validateMutation.isPending}
            data-testid="config-validate-btn"
          >
            {validateMutation.isPending
              ? t("config.validating")
              : t("config.validate")}
          </Button>
          <Button
            size="sm"
            onClick={() => saveMutation.mutate()}
            disabled={!initialized || saveMutation.isPending}
            data-testid="config-save-btn"
          >
            {saveMutation.isPending ? t("config.saving") : t("config.save")}
          </Button>
        </div>
      </header>

      <section className="grid grid-cols-1 gap-4 md:grid-cols-[180px_1fr]">
        <aside className="space-y-0.5 rounded-lg border border-border bg-panel p-2">
          <div className="px-2 pb-2 pt-1 text-[10px] uppercase tracking-wider text-muted-foreground">
            {t("config.sections")}
          </div>
          {SECTION_HEADERS.map((s) => (
            <button
              type="button"
              key={s}
              onClick={() => jumpToSection(s)}
              className={cn(
                "block w-full rounded px-2 py-1.5 text-left font-mono text-xs transition-colors",
                activeSection === s
                  ? "bg-accent text-accent-foreground"
                  : "text-muted-foreground hover:bg-accent/40 hover:text-foreground",
              )}
            >
              [{s}]
            </button>
          ))}
        </aside>
        <div className="overflow-hidden rounded-lg border border-border bg-panel">
          {config.isPending ? (
            <Skeleton className="h-[600px] w-full" />
          ) : config.isError ? (
            <div className="p-4 text-sm text-destructive">
              {t("config.loadFailed")}: {(config.error as Error).message}
            </div>
          ) : (
            <Editor
              height="600px"
              defaultLanguage="ini"
              value={draft}
              onChange={(v) => setDraft(v ?? "")}
              theme={resolvedTheme === "light" ? "vs-light" : "vs-dark"}
              onMount={onMount}
              options={{
                fontSize: 13,
                minimap: { enabled: false },
                wordWrap: "on",
                scrollBeyondLastLine: false,
                fontFamily: "var(--font-geist-mono), ui-monospace, monospace",
              }}
            />
          )}
        </div>
      </section>

      {/* save summary strip — always rendered so E2E `new version:` text lands */}
      {saveResult ? (
        <ResultStrip
          title={t("config.saveResult")}
          result={saveResult}
          kind="save"
        />
      ) : null}
      {validateResult ? (
        <ResultStrip
          title={t("config.validateResult")}
          result={validateResult}
          kind="validate"
        />
      ) : null}

      {validateMutation.isError ? (
        <p className="text-sm text-destructive">
          {t("config.validateFailed")}: {(validateMutation.error as Error).message}
        </p>
      ) : null}
      {saveMutation.isError ? (
        <p className="text-sm text-destructive">
          {t("common.saveFailed")}: {(saveMutation.error as Error).message}
        </p>
      ) : null}

      {/* issues slide-up panel */}
      <AnimatePresence>
        {issuesOpen && latestResult && latestResult.issues.length > 0 ? (
          <motion.div
            initial={{ opacity: 0, y: 16 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 16 }}
            transition={{ duration: 0.2, ease: "easeOut" }}
            className="fixed bottom-4 left-1/2 z-40 w-[min(720px,94vw)] -translate-x-1/2 rounded-lg border border-border bg-popover p-3 shadow-2xl"
            role="region"
            aria-label={t("config.validationIssuesRegion")}
          >
            <div className="mb-2 flex items-center justify-between">
              <div className="flex items-center gap-2">
                <AlertTriangle className="h-4 w-4 text-warn" />
                <span className="text-sm font-semibold">
                  {latestResult.issues.length === 1
                    ? t("config.issueTitleSingular")
                    : t("config.issueTitle", { n: latestResult.issues.length })}
                </span>
              </div>
              <button
                type="button"
                onClick={() => setIssuesOpen(false)}
                className="inline-flex h-6 w-6 items-center justify-center rounded text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
                aria-label={t("config.closeIssues")}
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
            <ul className="max-h-[30vh] space-y-1 overflow-auto">
              {latestResult.issues.map((iss, i) => (
                <li key={i} className="flex items-start gap-2 text-xs">
                  <Badge
                    variant={
                      iss.level === "error" ? "destructive" : "secondary"
                    }
                    className="shrink-0"
                  >
                    {iss.level}
                  </Badge>
                  <code className="shrink-0 font-mono text-muted-foreground">
                    {iss.path}
                  </code>
                  <span className="flex-1">{iss.message}</span>
                </li>
              ))}
            </ul>
          </motion.div>
        ) : null}
      </AnimatePresence>
    </>
  );
}

function ResultStrip({
  title,
  result,
  kind,
}: {
  title: string;
  result: ConfigPostResponse;
  kind: "validate" | "save";
}) {
  const { t } = useTranslation();
  return (
    <section
      className={cn(
        "flex flex-wrap items-center gap-3 rounded-lg border p-3 text-sm",
        kind === "save" && result.status === "ok"
          ? "border-ok/40 bg-ok/5"
          : "border-border bg-panel",
      )}
    >
      <span className="font-semibold">{title}</span>
      {result.status === "ok" ? (
        <Badge className="border-transparent bg-ok/15 text-ok">
          {t("config.statusOk")}
        </Badge>
      ) : (
        <Badge variant="destructive">{t("config.statusInvalid")}</Badge>
      )}
      {result.version ? (
        <code className="rounded bg-muted px-2 py-0.5 font-mono text-[11px]">
          {t("config.newVersion", { v: result.version })}
        </code>
      ) : null}
      {result.issues.length > 0 ? (
        <span className="text-xs text-muted-foreground">
          {result.issues.length === 1
            ? t("config.issueTitleSingular")
            : t("config.issueCount", { n: result.issues.length })}
        </span>
      ) : (
        <span className="text-xs text-muted-foreground">
          {t("config.noIssues")}
        </span>
      )}
      {result.requires_restart.length > 0 ? (
        <span className="text-xs text-warn">
          {t("config.restartRequired", {
            list: result.requires_restart.join(", "),
          })}
        </span>
      ) : null}
    </section>
  );
}
