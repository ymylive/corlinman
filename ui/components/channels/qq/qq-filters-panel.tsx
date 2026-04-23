"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import { Plus, X } from "lucide-react";

import { cn } from "@/lib/utils";
import { GlassPanel } from "@/components/ui/glass-panel";

/**
 * Right column of the QQ config grid — per-group keyword overrides.
 *
 * Each group is a row with:
 *   - mono chat_id chip
 *   - keyword tag-input (Enter = add, Backspace on empty = remove last, × = remove)
 *   - trailing "remove group" button
 *
 * An add-group row (input + submit) lives at the top. Save CTA sits in the
 * panel header.
 */

export interface QqFiltersPanelProps {
  draft: Record<string, string[]>;
  saving: boolean;
  dirty: boolean;
  onChange: (next: Record<string, string[]>) => void;
  onSave: () => void;
}

export function QqFiltersPanel({
  draft,
  saving,
  dirty,
  onChange,
  onSave,
}: QqFiltersPanelProps) {
  const { t } = useTranslation();
  const [addId, setAddId] = React.useState("");
  const entries = React.useMemo(
    () => Object.entries(draft).sort(([a], [b]) => a.localeCompare(b)),
    [draft],
  );

  const addGroup = (id: string) => {
    const trimmed = id.trim();
    if (!trimmed) return;
    if (draft[trimmed]) return;
    onChange({ ...draft, [trimmed]: [] });
    setAddId("");
  };

  const removeGroup = (id: string) => {
    const next = { ...draft };
    delete next[id];
    onChange(next);
  };

  const updateKeywords = (id: string, kws: string[]) => {
    onChange({ ...draft, [id]: kws });
  };

  return (
    <GlassPanel
      variant="soft"
      as="section"
      className="flex flex-col gap-4 p-5"
      data-testid="qq-filters-panel"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="text-[14px] font-medium text-tp-ink">
            {t("channels.groupKeywords")}
          </h2>
          <p className="mt-1 text-[12px] text-tp-ink-3">
            {t("channels.groupKeywordsHint")}
          </p>
        </div>
        <button
          type="button"
          onClick={onSave}
          disabled={saving || !dirty}
          data-testid="qq-save-keywords-btn"
          className={cn(
            "inline-flex shrink-0 items-center gap-2 rounded-lg border px-3 py-2 text-[13px] font-medium",
            "border-tp-amber/35 bg-tp-amber-soft text-tp-amber",
            "transition-colors hover:bg-[color-mix(in_oklch,var(--tp-amber)_22%,transparent)]",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/50",
            "disabled:cursor-not-allowed disabled:opacity-60",
          )}
        >
          {saving ? t("channels.saving") : t("channels.save")}
        </button>
      </div>

      {/* Add-group row. */}
      <form
        onSubmit={(e) => {
          e.preventDefault();
          addGroup(addId);
        }}
        className="flex items-center gap-2"
      >
        <input
          type="text"
          value={addId}
          onChange={(e) => setAddId(e.target.value)}
          placeholder={t("channels.qq.tp.addChatPlaceholder")}
          aria-label={t("channels.qq.tp.addChatPlaceholder")}
          className="h-9 flex-1 rounded-lg border border-tp-glass-edge bg-tp-glass-inner px-3 font-mono text-[12.5px] text-tp-ink placeholder:text-tp-ink-4 transition-colors hover:bg-tp-glass-inner-hover focus:outline-none focus:ring-2 focus:ring-tp-amber/40"
        />
        <button
          type="submit"
          disabled={!addId.trim()}
          className={cn(
            "inline-flex items-center gap-1.5 rounded-lg border border-tp-glass-edge bg-tp-glass-inner px-3 py-2 text-[12.5px] font-medium text-tp-ink-2 transition-colors",
            "hover:bg-tp-glass-inner-hover hover:text-tp-ink",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/40",
            "disabled:cursor-not-allowed disabled:opacity-50",
          )}
        >
          <Plus className="h-3.5 w-3.5" aria-hidden />
          {t("channels.addGroup")}
        </button>
      </form>

      {entries.length === 0 ? (
        <div
          className={cn(
            "rounded-xl border border-dashed border-tp-glass-edge bg-tp-glass-inner/60 p-6 text-center text-[12.5px] text-tp-ink-3",
          )}
        >
          {t("channels.noOverrides")}
        </div>
      ) : (
        <ul className="flex flex-col gap-2">
          {entries.map(([id, kws]) => (
            <GroupRow
              key={id}
              gid={id}
              keywords={kws}
              onChange={(next) => updateKeywords(id, next)}
              onRemove={() => removeGroup(id)}
            />
          ))}
        </ul>
      )}
    </GlassPanel>
  );
}

function GroupRow({
  gid,
  keywords,
  onChange,
  onRemove,
}: {
  gid: string;
  keywords: string[];
  onChange: (next: string[]) => void;
  onRemove: () => void;
}) {
  const { t } = useTranslation();
  const [draft, setDraft] = React.useState("");

  const add = (raw: string) => {
    const kw = raw.trim();
    if (!kw) return;
    if (keywords.includes(kw)) return;
    onChange([...keywords, kw]);
    setDraft("");
  };
  const remove = (kw: string) =>
    onChange(keywords.filter((x) => x !== kw));

  return (
    <li className="flex flex-wrap items-start gap-2 rounded-xl border border-tp-glass-edge bg-tp-glass-inner px-3 py-2">
      <code className="shrink-0 rounded-md border border-tp-glass-edge bg-tp-glass-inner-strong px-2 py-1 font-mono text-[11px] text-tp-ink-2">
        {gid}
      </code>
      <div className="flex min-h-[28px] flex-1 flex-wrap items-center gap-1.5">
        {keywords.map((kw) => (
          <button
            key={kw}
            type="button"
            onClick={() => remove(kw)}
            aria-label={t("channels.removeKeywordAria", { kw })}
            className={cn(
              "group/chip inline-flex items-center gap-1 rounded-md border px-2 py-[2px] font-mono text-[10.5px]",
              "border-tp-amber/30 bg-tp-amber-soft text-tp-amber",
              "transition-colors",
              "hover:bg-[color-mix(in_oklch,var(--tp-amber)_22%,transparent)]",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/50",
            )}
          >
            {kw}
            <X className="h-3 w-3 opacity-70 group-hover/chip:opacity-100" aria-hidden />
          </button>
        ))}
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              add(draft);
            } else if (
              e.key === "Backspace" &&
              !draft &&
              keywords.length > 0
            ) {
              e.preventDefault();
              remove(keywords[keywords.length - 1]!);
            }
          }}
          placeholder={t("channels.addKeywordPlaceholder")}
          aria-label={t("channels.addKeywordPlaceholder")}
          className="h-7 min-w-[140px] flex-1 bg-transparent px-1 font-mono text-[11px] text-tp-ink placeholder:text-tp-ink-4 focus:outline-none"
        />
      </div>
      <button
        type="button"
        onClick={onRemove}
        aria-label={t("channels.qq.tp.removeChatAria", { id: gid })}
        className={cn(
          "inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-tp-ink-3",
          "transition-colors hover:bg-tp-glass-inner-hover hover:text-tp-err",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-err/40",
        )}
      >
        <X className="h-3.5 w-3.5" aria-hidden />
      </button>
    </li>
  );
}

export default QqFiltersPanel;
