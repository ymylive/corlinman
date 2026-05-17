"use client";

/**
 * EnvVarRow — a single editable credential field inside a
 * `[providers.<name>]` block.
 *
 * Three visual states, gated by props:
 *
 *   1. **unset** — the operator never wrote this key. Shows the key
 *      label, the conventional env-var hint, and an "Add" button that
 *      flips the row into editing mode.
 *   2. **set** — value is configured. Shows a masked preview (the
 *      server only ever returns "…last4") with eye-icon reveal,
 *      replace, and trash buttons.
 *   3. **editing** — a password-type Input, paste-only handler, Save
 *      and Cancel. Paste is allowed via `onPaste`; key-typing is
 *      tolerated but a one-time toast nudges the operator to paste
 *      instead so muscle-memory typos can't slip into the TOML.
 *
 * Borrowed from hermes-agent `web/src/pages/EnvPage.tsx:99-160`. The
 * key shape difference: corlinman's gateway never returns plaintext, so
 * the reveal action simply un-greys the existing "…last4" preview
 * rather than revealing the full value.
 */

import * as React from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { Check, Eye, EyeOff, Pencil, Trash2, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import type { CredentialField } from "@/lib/api";
import { cn } from "@/lib/utils";

export interface EnvVarRowProps {
  provider: string;
  field: CredentialField;
  /** Pretty label for the field — defaults to the raw key. */
  label?: string;
  saving?: boolean;
  onSave: (value: string) => void | Promise<void>;
  onDelete: () => void | Promise<void>;
  /** Optional override id prefix for nested data-testid attributes. */
  testIdPrefix?: string;
}

export function EnvVarRow({
  provider,
  field,
  label,
  saving = false,
  onSave,
  onDelete,
  testIdPrefix,
}: EnvVarRowProps) {
  const { t } = useTranslation();
  const [editing, setEditing] = React.useState(false);
  const [value, setValue] = React.useState("");
  const [revealed, setRevealed] = React.useState(false);
  const [typeWarned, setTypeWarned] = React.useState(false);

  const prefix = testIdPrefix ?? `cred-${provider}-${field.key}`;
  const displayLabel = label ?? field.key;

  // Reset edit buffer + reveal state whenever the field flips between
  // set/unset (e.g. external refetch after save). Without this, the
  // input would retain a stale value across mounts of the same row.
  React.useEffect(() => {
    if (!editing) setValue("");
    setRevealed(false);
  }, [editing, field.set, field.preview]);

  async function handleSave() {
    if (!value) return;
    await onSave(value);
    setEditing(false);
    setValue("");
    setTypeWarned(false);
  }

  function handleCancel() {
    setEditing(false);
    setValue("");
    setTypeWarned(false);
  }

  // -- editing --
  if (editing) {
    return (
      <div
        className="flex items-center gap-2 rounded-md border border-tp-glass-edge bg-tp-glass-inner/40 px-3 py-2"
        data-testid={`${prefix}-row`}
      >
        <Label
          htmlFor={`${prefix}-input`}
          className="w-32 shrink-0 font-mono text-[11px] text-tp-ink-3"
        >
          {displayLabel}
        </Label>
        <Input
          id={`${prefix}-input`}
          data-testid={`${prefix}-input`}
          type="password"
          autoFocus
          autoComplete="off"
          spellCheck={false}
          placeholder={t("credentials.pastePlaceholder")}
          value={value}
          onPaste={(e) => {
            // Pasting is the intended path; we still let onChange fire
            // so the Save button activates without an extra render.
            const pasted = e.clipboardData.getData("text");
            if (pasted) {
              e.preventDefault();
              setValue(pasted.trim());
            }
          }}
          onChange={(e) => {
            const next = e.target.value;
            // First non-paste keystroke surfaces a soft nudge so the
            // operator notices the paste-only pattern. We don't block —
            // some keyboards (and some passwords) really do need typing.
            if (!typeWarned && next.length === 1 && !value) {
              toast.message(t("credentials.pasteHint"));
              setTypeWarned(true);
            }
            setValue(next);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              void handleSave();
            } else if (e.key === "Escape") {
              e.preventDefault();
              handleCancel();
            }
          }}
          className="h-8 flex-1 font-mono text-xs"
          disabled={saving}
        />
        <Button
          size="sm"
          data-testid={`${prefix}-save`}
          disabled={saving || !value}
          onClick={() => void handleSave()}
          aria-label={t("common.save")}
        >
          <Check className="h-3.5 w-3.5" />
        </Button>
        <Button
          size="sm"
          variant="ghost"
          data-testid={`${prefix}-cancel`}
          disabled={saving}
          onClick={handleCancel}
          aria-label={t("common.cancel")}
        >
          <X className="h-3.5 w-3.5" />
        </Button>
      </div>
    );
  }

  // -- unset --
  if (!field.set) {
    return (
      <div
        className="flex items-center gap-3 rounded-md border border-dashed border-tp-glass-edge px-3 py-2 opacity-75 transition-opacity hover:opacity-100"
        data-testid={`${prefix}-row`}
      >
        <Label className="w-32 shrink-0 font-mono text-[11px] text-tp-ink-3">
          {displayLabel}
        </Label>
        <div className="flex-1 truncate text-[11px] text-tp-ink-3">
          {field.env_ref ? (
            <span className="font-mono">
              {t("credentials.envHint", { env: field.env_ref })}
            </span>
          ) : (
            <span>{t("credentials.fieldUnset")}</span>
          )}
        </div>
        <Button
          size="sm"
          variant="outline"
          data-testid={`${prefix}-add`}
          onClick={() => setEditing(true)}
        >
          <Pencil className="h-3 w-3" />
          {t("credentials.addValue")}
        </Button>
      </div>
    );
  }

  // -- set --
  return (
    <div
      className="flex items-center gap-2 rounded-md border border-tp-glass-edge px-3 py-2"
      data-testid={`${prefix}-row`}
    >
      <Label className="w-32 shrink-0 font-mono text-[11px] text-tp-ink-2">
        {displayLabel}
      </Label>
      <div
        data-testid={`${prefix}-preview`}
        className={cn(
          "flex-1 truncate rounded border border-tp-glass-edge bg-tp-glass-inner/40 px-2 py-1 font-mono text-[11px]",
          revealed ? "text-tp-ink" : "text-tp-ink-3",
        )}
      >
        {field.preview ? (
          revealed ? (
            <span data-testid={`${prefix}-preview-revealed`}>
              {field.preview}
            </span>
          ) : (
            <span aria-hidden>{"•".repeat(8)}</span>
          )
        ) : field.env_ref ? (
          <span className="text-tp-ink-3">env: {field.env_ref}</span>
        ) : (
          <span className="text-tp-ink-3">{t("credentials.fieldSet")}</span>
        )}
      </div>
      {field.preview ? (
        <Button
          size="sm"
          variant="ghost"
          data-testid={`${prefix}-reveal`}
          aria-label={
            revealed ? t("credentials.hideValue") : t("credentials.revealValue")
          }
          aria-pressed={revealed}
          onClick={() => setRevealed((r) => !r)}
        >
          {revealed ? (
            <EyeOff className="h-3.5 w-3.5" />
          ) : (
            <Eye className="h-3.5 w-3.5" />
          )}
        </Button>
      ) : null}
      <Button
        size="sm"
        variant="outline"
        data-testid={`${prefix}-replace`}
        onClick={() => setEditing(true)}
      >
        <Pencil className="h-3 w-3" />
        {t("credentials.replaceValue")}
      </Button>
      <Button
        size="sm"
        variant="ghost"
        data-testid={`${prefix}-delete`}
        aria-label={t("common.delete")}
        disabled={saving}
        onClick={() => void onDelete()}
        className="text-destructive hover:text-destructive"
      >
        <Trash2 className="h-3.5 w-3.5" />
      </Button>
    </div>
  );
}

export default EnvVarRow;
