"use client";

/**
 * /credentials — provider-credential manager (Wave 2.3).
 *
 * Builds on top of `/admin/credentials*` (see
 * `gateway/routes_admin_b/credentials.py`). Borrows hermes-agent's
 * EnvPage UX:
 *   - provider-grouped sections (collapsed-by-default when empty),
 *   - per-row eye-icon reveal of the "…last4" preview,
 *   - paste-only inputs with a soft "paste, don't type" nudge,
 *   - destructive ops gated behind a confirmation dialog,
 *   - toasts on every mutation.
 *
 * Plaintext values never leave the gateway; the page only ever asks the
 * server to redact + return previews. Reveal toggles the masked display
 * between "••••••••" and "…xyz9", never the full literal.
 */

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { KeyRound, Plug, Search } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { Badge } from "@/components/ui/badge";
import { EnvVarRow } from "@/components/credentials/env-var-row";
import {
  CorlinmanApiError,
  deleteCredential,
  listCredentials,
  setCredential,
  setProviderEnabled,
  type CredentialProvider,
} from "@/lib/api";

const FIELD_LABEL_KEYS: Record<string, string> = {
  api_key: "credentials.fieldKeyApiKey",
  base_url: "credentials.fieldKeyBaseUrl",
  org_id: "credentials.fieldKeyOrgId",
  kind: "credentials.fieldKeyKind",
};

function isProviderConfigured(p: CredentialProvider): boolean {
  return p.fields.some((f) => f.set);
}

export default function CredentialsPage() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [search, setSearch] = React.useState("");
  const [showEmpty, setShowEmpty] = React.useState(true);
  const [pendingDelete, setPendingDelete] = React.useState<{
    provider: string;
    key: string;
  } | null>(null);

  const credentials = useQuery({
    queryKey: ["admin", "credentials"],
    queryFn: listCredentials,
    retry: false,
  });

  const saveField = useMutation({
    mutationFn: async (vars: {
      provider: string;
      key: string;
      value: string;
    }) => setCredential(vars.provider, vars.key, vars.value),
    onSuccess: (_data, vars) => {
      toast.success(t("credentials.fieldSaved", { key: vars.key }));
      qc.invalidateQueries({ queryKey: ["admin", "credentials"] });
    },
    onError: (err, vars) => {
      if (err instanceof CorlinmanApiError && err.status === 400) {
        toast.error(t("credentials.unknownField", { key: vars.key }));
        return;
      }
      toast.error(
        t("credentials.saveFailed", {
          msg: err instanceof Error ? err.message : String(err),
        }),
      );
    },
  });

  const removeField = useMutation({
    mutationFn: async (vars: { provider: string; key: string }) =>
      deleteCredential(vars.provider, vars.key),
    onSuccess: (_data, vars) => {
      toast.success(t("credentials.fieldDeleted", { key: vars.key }));
      setPendingDelete(null);
      qc.invalidateQueries({ queryKey: ["admin", "credentials"] });
    },
    onError: (err) => {
      toast.error(
        t("credentials.deleteFailed", {
          msg: err instanceof Error ? err.message : String(err),
        }),
      );
    },
  });

  const toggleProvider = useMutation({
    mutationFn: async (vars: { provider: string; enabled: boolean }) =>
      setProviderEnabled(vars.provider, vars.enabled),
    onSuccess: (_data, vars) => {
      toast.success(
        t(
          vars.enabled
            ? "credentials.providerEnabled"
            : "credentials.providerDisabled",
          { provider: vars.provider },
        ),
      );
      qc.invalidateQueries({ queryKey: ["admin", "credentials"] });
    },
    onError: (err) => {
      toast.error(
        t("credentials.saveFailed", {
          msg: err instanceof Error ? err.message : String(err),
        }),
      );
    },
  });

  const providers = credentials.data?.providers ?? [];

  const filtered = React.useMemo(() => {
    const q = search.trim().toLowerCase();
    return providers.filter((p) => {
      if (!showEmpty && !isProviderConfigured(p)) return false;
      if (!q) return true;
      return (
        p.name.toLowerCase().includes(q) || p.kind.toLowerCase().includes(q)
      );
    });
  }, [providers, search, showEmpty]);

  const total = providers.length;
  const configured = providers.filter(isProviderConfigured).length;

  return (
    <div className="flex flex-col gap-6">
      <header className="flex flex-col gap-1">
        <div className="flex items-center gap-2">
          <KeyRound className="h-5 w-5 text-tp-ink-3" aria-hidden />
          <h1 className="text-2xl font-semibold tracking-tight">
            {t("credentials.title")}
          </h1>
        </div>
        <p className="text-sm text-tp-ink-3">{t("credentials.subtitle")}</p>
        <p
          className="text-xs text-tp-ink-3"
          data-testid="credentials-count-summary"
        >
          {t("credentials.countSummary", { total, configured })}
        </p>
      </header>

      <div className="flex flex-wrap items-center gap-3">
        <div className="relative flex-1 min-w-[200px] max-w-md">
          <Search
            className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-tp-ink-3"
            aria-hidden
          />
          <Input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t("credentials.search")}
            className="pl-8"
            data-testid="credentials-search"
          />
        </div>
        <label className="flex items-center gap-2 text-xs text-tp-ink-2">
          <Switch
            checked={showEmpty}
            onCheckedChange={setShowEmpty}
            aria-label={t("credentials.showEmpty")}
            data-testid="credentials-show-empty"
          />
          <span>{t("credentials.showEmpty")}</span>
        </label>
      </div>

      {credentials.isPending ? (
        <Skeleton className="h-40 w-full" />
      ) : credentials.isError ? (
        <p className="text-xs text-destructive" data-testid="credentials-error">
          {t("credentials.loadFailed")}:{" "}
          {credentials.error instanceof Error
            ? credentials.error.message
            : String(credentials.error)}
        </p>
      ) : filtered.length === 0 ? (
        <Card data-testid="credentials-empty">
          <CardContent className="flex flex-col items-center gap-2 py-10 text-center">
            <Plug className="h-6 w-6 text-tp-ink-3" aria-hidden />
            <p className="text-sm text-tp-ink-3">
              {t("credentials.emptyState")}
            </p>
          </CardContent>
        </Card>
      ) : (
        <div className="flex flex-col gap-4">
          {filtered.map((p) => {
            const configuredFields = p.fields.filter((f) => f.set).length;
            const totalFields = p.fields.length;
            return (
              <Card
                key={p.name}
                data-testid={`credentials-provider-${p.name}`}
              >
                <CardHeader className="border-b border-tp-glass-edge">
                  <div className="flex items-center justify-between gap-3">
                    <div className="flex items-center gap-2">
                      <CardTitle className="text-base capitalize">
                        {p.name}
                      </CardTitle>
                      <Badge variant="secondary" className="font-mono text-[10px]">
                        {p.kind}
                      </Badge>
                      {p.enabled ? (
                        <Badge className="border-transparent bg-ok/15 text-ok">
                          {t("common.enabled")}
                        </Badge>
                      ) : (
                        <Badge variant="secondary">{t("common.disabled")}</Badge>
                      )}
                    </div>
                    <div className="flex items-center gap-3">
                      <CardDescription
                        data-testid={`credentials-provider-${p.name}-count`}
                      >
                        {t("credentials.countConfigured", {
                          configured: configuredFields,
                          total: totalFields,
                        })}
                      </CardDescription>
                      <Switch
                        checked={p.enabled}
                        onCheckedChange={(next) =>
                          toggleProvider.mutate({
                            provider: p.name,
                            enabled: next,
                          })
                        }
                        aria-label={
                          p.enabled
                            ? t("credentials.providerDisabled", {
                                provider: p.name,
                              })
                            : t("credentials.providerEnabled", {
                                provider: p.name,
                              })
                        }
                        data-testid={`credentials-provider-${p.name}-toggle`}
                      />
                    </div>
                  </div>
                </CardHeader>
                <CardContent className="grid gap-2 pt-3">
                  {p.fields.length === 0 ? (
                    <p className="text-[11px] text-tp-ink-3">
                      {t("credentials.fieldUnset")}
                    </p>
                  ) : (
                    p.fields.map((f) => {
                      const labelKey = FIELD_LABEL_KEYS[f.key];
                      return (
                        <EnvVarRow
                          key={f.key}
                          provider={p.name}
                          field={f}
                          label={labelKey ? t(labelKey) : f.key}
                          saving={
                            (saveField.isPending &&
                              saveField.variables?.provider === p.name &&
                              saveField.variables?.key === f.key) ||
                            (removeField.isPending &&
                              removeField.variables?.provider === p.name &&
                              removeField.variables?.key === f.key)
                          }
                          onSave={async (value) => {
                            await saveField.mutateAsync({
                              provider: p.name,
                              key: f.key,
                              value,
                            });
                          }}
                          onDelete={() =>
                            setPendingDelete({
                              provider: p.name,
                              key: f.key,
                            })
                          }
                        />
                      );
                    })
                  )}
                </CardContent>
              </Card>
            );
          })}
        </div>
      )}

      <Dialog
        open={!!pendingDelete}
        onOpenChange={(o) => {
          if (!o) setPendingDelete(null);
        }}
      >
        <DialogContent data-testid="credentials-delete-dialog">
          <DialogHeader>
            <DialogTitle>
              {pendingDelete
                ? t("credentials.deleteConfirmTitle", {
                    provider: pendingDelete.provider,
                    key: pendingDelete.key,
                  })
                : ""}
            </DialogTitle>
            <DialogDescription>
              {pendingDelete
                ? t("credentials.deleteConfirm", {
                    provider: pendingDelete.provider,
                    key: pendingDelete.key,
                  })
                : ""}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setPendingDelete(null)}
              data-testid="credentials-delete-cancel"
            >
              {t("common.cancel")}
            </Button>
            <Button
              variant="destructive"
              data-testid="credentials-delete-confirm"
              disabled={removeField.isPending}
              onClick={() => {
                if (pendingDelete) removeField.mutate(pendingDelete);
              }}
            >
              {t("common.delete")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
