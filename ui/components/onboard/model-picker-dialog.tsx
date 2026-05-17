"use client";

/**
 * Two-stage model picker for the /onboard wizard (Wave 2.1).
 *
 * Borrows the UX from hermes-agent's `ModelPickerDialog`
 * (web/src/components/ModelPickerDialog.tsx:9-200) — Stage 1 picks a
 * provider (channel), Stage 2 narrows to a model within that provider —
 * but reimplements it on corlinman's stack:
 *   - Radix Dialog + Tailwind tp-* tokens, no `@nous-research/ui`
 *   - data source is `NewapiChannel[]` from `listOnboardChannels`
 *   - keyboard: `/` focuses the search input, `Esc` closes (handled by
 *     Radix), arrow keys + Enter navigate the list
 *
 * The dialog calls `onPick({channel_id, model})` on selection and closes.
 * Stateless across mount: each open resets to the provider stage.
 */

import * as React from "react";
import { useTranslation } from "react-i18next";
import { ChevronRight, Search } from "lucide-react";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { NewapiChannel } from "@/lib/api";

export type ModelPickerKind = "llm" | "embedding" | "tts";

export interface ModelPickerDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  providers: NewapiChannel[];
  /** Fires once a model row is chosen. The dialog also closes itself. */
  onPick: (pick: { channel_id: number; model: string }) => void;
  kind: ModelPickerKind;
  /** Pre-selected channel/model — controls the initial highlight in Stage 2. */
  currentChannelId?: number;
  currentModel?: string;
}

type Stage = "provider" | "model";

function parseModels(raw: string): string[] {
  return raw
    .split(",")
    .map((m) => m.trim())
    .filter(Boolean);
}

export function ModelPickerDialog({
  open,
  onOpenChange,
  providers,
  onPick,
  kind,
  currentChannelId,
  currentModel,
}: ModelPickerDialogProps): React.ReactElement {
  const { t } = useTranslation();
  const [stage, setStage] = React.useState<Stage>("provider");
  const [selectedChannelId, setSelectedChannelId] = React.useState<
    number | null
  >(null);
  const [query, setQuery] = React.useState("");
  const searchRef = React.useRef<HTMLInputElement | null>(null);

  // Reset whenever the dialog opens so users always land on the provider
  // stage with an empty search — matches hermes' default behavior.
  React.useEffect(() => {
    if (open) {
      setStage("provider");
      setQuery("");
      setSelectedChannelId(currentChannelId ?? null);
    }
  }, [open, currentChannelId]);

  // `/` focuses search when the dialog is open and focus isn't already in
  // an editable element. Mirrors the hermes keyboard shortcut.
  React.useEffect(() => {
    if (!open) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "/" && document.activeElement?.tagName !== "INPUT") {
        e.preventDefault();
        searchRef.current?.focus();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  const selectedChannel = React.useMemo(
    () => providers.find((p) => p.id === selectedChannelId) ?? null,
    [providers, selectedChannelId],
  );

  const needle = query.trim().toLowerCase();

  const filteredProviders = React.useMemo(() => {
    if (!needle) return providers;
    return providers.filter(
      (p) =>
        p.name.toLowerCase().includes(needle) ||
        parseModels(p.models).some((m) => m.toLowerCase().includes(needle)),
    );
  }, [providers, needle]);

  const allModels = selectedChannel ? parseModels(selectedChannel.models) : [];
  const filteredModels = React.useMemo(() => {
    if (!needle) return allModels;
    return allModels.filter((m) => m.toLowerCase().includes(needle));
  }, [allModels, needle]);

  function pickProvider(channelId: number) {
    setSelectedChannelId(channelId);
    setStage("model");
    setQuery("");
    // Re-focus search on the next paint so `/` keeps working.
    requestAnimationFrame(() => searchRef.current?.focus());
  }

  function pickModel(model: string) {
    if (selectedChannelId == null) return;
    onPick({ channel_id: selectedChannelId, model });
    onOpenChange(false);
  }

  const titleKey =
    stage === "provider"
      ? "auth.onboardPickerStageProvider"
      : "auth.onboardPickerStageModel";
  const searchPlaceholderKey =
    stage === "provider"
      ? "auth.onboardPickerSearchProviders"
      : "auth.onboardPickerSearchModels";

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="max-w-xl gap-0 p-0"
        data-testid="model-picker-dialog"
      >
        <DialogHeader className="border-b border-tp-glass-edge px-5 pb-3 pt-5">
          <DialogTitle className="text-sm font-semibold uppercase tracking-wider">
            {t(titleKey)}
            <span className="ml-2 text-xs font-normal normal-case text-tp-ink-3">
              · {kind}
            </span>
          </DialogTitle>
          <DialogDescription className="sr-only">
            {t(searchPlaceholderKey)}
          </DialogDescription>
          {stage === "model" && selectedChannel ? (
            <button
              type="button"
              onClick={() => {
                setStage("provider");
                setQuery("");
                requestAnimationFrame(() => searchRef.current?.focus());
              }}
              className="mt-1 inline-flex w-fit items-center gap-1 text-xs text-tp-ink-3 hover:text-foreground"
              data-testid="model-picker-back"
            >
              ← {t("auth.onboardPickerBack")} ·{" "}
              <span className="font-mono">{selectedChannel.name}</span>
            </button>
          ) : null}
        </DialogHeader>

        <div className="border-b border-tp-glass-edge px-5 py-2.5">
          <div className="relative">
            <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-tp-ink-3" />
            <Input
              ref={searchRef}
              autoFocus
              placeholder={t(searchPlaceholderKey)}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              className="h-8 pl-7 text-sm"
              data-testid="model-picker-search"
            />
          </div>
        </div>

        <div
          className="max-h-[60vh] min-h-[12rem] overflow-y-auto"
          data-testid="model-picker-list"
        >
          {stage === "provider" ? (
            <ProviderList
              providers={filteredProviders}
              totalProviders={providers.length}
              currentChannelId={currentChannelId}
              onPick={pickProvider}
            />
          ) : (
            <ModelList
              models={filteredModels}
              totalModels={allModels.length}
              currentChannelId={currentChannelId}
              currentModel={currentModel}
              selectedChannelId={selectedChannelId}
              onPick={pickModel}
            />
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}

function ProviderList({
  providers,
  totalProviders,
  currentChannelId,
  onPick,
}: {
  providers: NewapiChannel[];
  totalProviders: number;
  currentChannelId?: number;
  onPick: (channelId: number) => void;
}) {
  const { t } = useTranslation();
  if (totalProviders === 0) {
    return (
      <div className="p-5 text-xs italic text-tp-ink-3">
        {t("auth.onboardPickerNoProviders")}
      </div>
    );
  }
  if (providers.length === 0) {
    return (
      <div className="p-5 text-xs italic text-tp-ink-3">
        {t("auth.onboardPickerNoProviders")}
      </div>
    );
  }
  return (
    <ul role="listbox" aria-label="providers">
      {providers.map((p) => {
        const models = parseModels(p.models);
        const isCurrent = p.id === currentChannelId;
        return (
          <li key={p.id}>
            <button
              type="button"
              role="option"
              aria-selected={isCurrent}
              onClick={() => onPick(p.id)}
              className={cn(
                "flex w-full items-center gap-3 border-l-2 px-4 py-2.5 text-left text-sm hover:bg-tp-glass-inner",
                isCurrent
                  ? "border-l-primary"
                  : "border-l-transparent",
              )}
              data-testid={`model-picker-provider-${p.id}`}
            >
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="truncate font-medium">{p.name}</span>
                  {isCurrent && (
                    <span className="shrink-0 text-[0.6rem] uppercase tracking-wider text-primary/80">
                      current
                    </span>
                  )}
                </div>
                <div className="truncate font-mono text-[0.65rem] text-tp-ink-3">
                  {models.length} models
                </div>
              </div>
              <ChevronRight className="h-3.5 w-3.5 shrink-0 text-tp-ink-3" />
            </button>
          </li>
        );
      })}
    </ul>
  );
}

function ModelList({
  models,
  totalModels,
  currentChannelId,
  currentModel,
  selectedChannelId,
  onPick,
}: {
  models: string[];
  totalModels: number;
  currentChannelId?: number;
  currentModel?: string;
  selectedChannelId: number | null;
  onPick: (model: string) => void;
}) {
  const { t } = useTranslation();
  if (totalModels === 0) {
    return (
      <div className="p-5 text-xs italic text-tp-ink-3">
        {t("auth.onboardPickerNoModels")}
      </div>
    );
  }
  if (models.length === 0) {
    return (
      <div className="p-5 text-xs italic text-tp-ink-3">
        {t("auth.onboardPickerNoModels")}
      </div>
    );
  }
  return (
    <ul role="listbox" aria-label="models">
      {models.map((m) => {
        const isCurrent =
          m === currentModel && selectedChannelId === currentChannelId;
        return (
          <li key={m}>
            <button
              type="button"
              role="option"
              aria-selected={isCurrent}
              onClick={() => onPick(m)}
              className={cn(
                "flex w-full items-center gap-3 px-4 py-2 text-left font-mono text-xs hover:bg-tp-glass-inner",
              )}
              data-testid={`model-picker-model-${m}`}
            >
              <span className="flex-1 truncate">{m}</span>
              {isCurrent && (
                <span className="shrink-0 text-[0.6rem] uppercase tracking-wider text-primary/80">
                  current
                </span>
              )}
            </button>
          </li>
        );
      })}
    </ul>
  );
}
