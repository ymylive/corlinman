"use client";

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { Check, Key, Pencil, Plus, Trash2, X } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { cn } from "@/lib/utils";
import { fetchModels, updateAliases, type ModelsResponse } from "@/lib/api";

/**
 * Models admin page. Providers rendered as cards (enabled toggle is
 * informational — the actual provider on/off flips in the config editor).
 * Aliases are an inline-edit table: click the alias cell to rename, click
 * the target cell to point it elsewhere.
 */
export default function ModelsPage() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const models = useQuery<ModelsResponse>({
    queryKey: ["admin", "models"],
    queryFn: fetchModels,
  });

  const [aliases, setAliases] = React.useState<Array<[string, string]>>([]);
  const [defaultModel, setDefaultModel] = React.useState("");
  const [initialized, setInitialized] = React.useState(false);
  React.useEffect(() => {
    if (models.data && !initialized) {
      setAliases(Object.entries(models.data.aliases));
      setDefaultModel(models.data.default);
      setInitialized(true);
    }
  }, [models.data, initialized]);

  const saveMutation = useMutation({
    mutationFn: () => {
      const map: Record<string, string> = {};
      for (const [k, v] of aliases) {
        if (k.trim() && v.trim()) map[k.trim()] = v.trim();
      }
      return updateAliases(map, defaultModel.trim() || undefined);
    },
    onSuccess: () => {
      toast.success(t("models.saveSuccess"));
      qc.invalidateQueries({ queryKey: ["admin", "models"] });
    },
    onError: (err) =>
      toast.error(
        t("models.saveFailed", {
          msg: err instanceof Error ? err.message : String(err),
        }),
      ),
  });

  return (
    <>
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">
          {t("models.title")}
        </h1>
        <p className="text-sm text-muted-foreground">{t("models.subtitle")}</p>
      </header>

      <section className="space-y-2">
        <h2 className="text-sm font-semibold">{t("models.providers")}</h2>
        {models.isPending ? (
          <Skeleton className="h-24 w-full" />
        ) : models.data && models.data.providers.length === 0 ? (
          <p className="rounded-md border border-dashed border-border p-6 text-center text-sm text-muted-foreground">
            {t("models.providersEmpty")}
          </p>
        ) : (
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
            {models.data?.providers.map((p) => (
              <div
                key={p.name}
                className={cn(
                  "flex flex-col gap-2 rounded-lg border p-4 transition-colors",
                  p.enabled
                    ? "border-border bg-panel hover:border-primary/40"
                    : "border-border bg-surface/60",
                )}
              >
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <span
                      className={cn(
                        "inline-block h-2 w-2 rounded-full",
                        p.enabled ? "bg-ok" : "bg-muted-foreground/40",
                      )}
                    />
                    <span className="text-sm font-semibold">{p.name}</span>
                  </div>
                  {p.enabled ? (
                    <Badge className="border-transparent bg-ok/15 text-ok">
                      {t("common.enabled")}
                    </Badge>
                  ) : (
                    <Badge variant="secondary">{t("common.disabled")}</Badge>
                  )}
                </div>
                <div className="flex items-center gap-2 text-xs">
                  <Key className="h-3 w-3 text-muted-foreground" />
                  {p.has_api_key ? (
                    <span className="font-mono text-muted-foreground">
                      {t("models.keyKind", { kind: p.api_key_kind })}
                    </span>
                  ) : (
                    <span className="text-destructive">
                      {t("models.keyMissing")}
                    </span>
                  )}
                </div>
                <div className="font-mono text-[11px] text-muted-foreground">
                  {p.base_url ?? t("models.providerDefault")}
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      <section className="space-y-3 rounded-lg border border-border bg-panel p-4">
        <div className="flex items-center justify-between gap-2">
          <div>
            <h2 className="text-sm font-semibold">{t("models.aliases")}</h2>
            <p className="text-xs text-muted-foreground">
              {t("models.aliasesHint")}
            </p>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-[10px] uppercase tracking-wider text-muted-foreground">
              {t("models.defaultLabel")}
            </span>
            <Input
              value={defaultModel}
              onChange={(e) => setDefaultModel(e.target.value)}
              className="h-8 w-48 font-mono text-xs"
              placeholder="claude-sonnet-4-5"
            />
            <Button
              size="sm"
              variant="outline"
              onClick={() => setAliases([...aliases, ["", ""]])}
            >
              <Plus className="h-3 w-3" />
              {t("models.addAlias")}
            </Button>
            <Button
              size="sm"
              onClick={() => saveMutation.mutate()}
              disabled={saveMutation.isPending}
              data-testid="models-save-btn"
            >
              {saveMutation.isPending ? t("models.saving") : t("models.save")}
            </Button>
          </div>
        </div>
        <Table>
          <TableHeader>
            <TableRow className="border-b border-border hover:bg-transparent">
              <TableHead className="w-52 pl-3">
                {t("models.aliasHeader")}
              </TableHead>
              <TableHead>{t("models.aliasTargetHeader")}</TableHead>
              <TableHead className="w-16"></TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {aliases.length === 0 ? (
              <TableRow>
                <TableCell
                  colSpan={3}
                  className="py-6 text-center text-sm text-muted-foreground"
                >
                  {t("models.noAliases")}
                </TableCell>
              </TableRow>
            ) : (
              aliases.map(([alias, target], idx) => (
                <AliasRow
                  key={idx}
                  alias={alias}
                  target={target}
                  onChange={(next) => {
                    const all = [...aliases];
                    all[idx] = next;
                    setAliases(all);
                  }}
                  onRemove={() =>
                    setAliases(aliases.filter((_, i) => i !== idx))
                  }
                />
              ))
            )}
          </TableBody>
        </Table>
        {saveMutation.isError ? (
          <p className="text-xs text-destructive">
            {(saveMutation.error as Error).message}
          </p>
        ) : saveMutation.isSuccess ? (
          <p className="text-xs text-ok">{t("models.aliasSavedInline")}</p>
        ) : null}
      </section>
    </>
  );
}

/** Inline-edit row. Cell is a span by default; click → input. Enter commits, Esc reverts. */
function AliasRow({
  alias,
  target,
  onChange,
  onRemove,
}: {
  alias: string;
  target: string;
  onChange: (next: [string, string]) => void;
  onRemove: () => void;
}) {
  const { t } = useTranslation();
  return (
    <TableRow className="border-b border-border">
      <TableCell className="pl-3">
        <InlineEdit
          value={alias}
          onCommit={(v) => onChange([v, target])}
          placeholder="smart"
          mono
        />
      </TableCell>
      <TableCell>
        <InlineEdit
          value={target}
          onCommit={(v) => onChange([alias, v])}
          placeholder="claude-opus-4-7"
          mono
        />
      </TableCell>
      <TableCell>
        <Button
          size="sm"
          variant="ghost"
          onClick={onRemove}
          aria-label={t("models.remove")}
        >
          <Trash2 className="h-3.5 w-3.5" />
        </Button>
      </TableCell>
    </TableRow>
  );
}

function InlineEdit({
  value,
  onCommit,
  placeholder,
  mono,
}: {
  value: string;
  onCommit: (v: string) => void;
  placeholder?: string;
  mono?: boolean;
}) {
  const { t } = useTranslation();
  const [editing, setEditing] = React.useState(!value);
  const [draft, setDraft] = React.useState(value);
  React.useEffect(() => {
    if (!editing) setDraft(value);
  }, [value, editing]);
  if (!editing) {
    return (
      <button
        type="button"
        onClick={() => setEditing(true)}
        className={cn(
          "group inline-flex h-8 w-full items-center justify-between gap-1 rounded px-2 text-left transition-colors hover:bg-accent/40",
          mono && "font-mono text-xs",
        )}
      >
        <span className={!value ? "text-muted-foreground" : ""}>
          {value || placeholder || t("models.emptyValue")}
        </span>
        <Pencil className="h-3 w-3 text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100" />
      </button>
    );
  }
  return (
    <div className="inline-flex w-full items-center gap-1">
      <Input
        autoFocus
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        placeholder={placeholder}
        className={cn("h-8", mono && "font-mono text-xs")}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            onCommit(draft);
            setEditing(false);
          } else if (e.key === "Escape") {
            setDraft(value);
            setEditing(false);
          }
        }}
      />
      <button
        type="button"
        onClick={() => {
          onCommit(draft);
          setEditing(false);
        }}
        className="inline-flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
        aria-label={t("models.commit")}
      >
        <Check className="h-3.5 w-3.5" />
      </button>
      <button
        type="button"
        onClick={() => {
          setDraft(value);
          setEditing(false);
        }}
        className="inline-flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
        aria-label={t("models.cancel")}
      >
        <X className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}
