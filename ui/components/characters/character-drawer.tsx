"use client";

import * as React from "react";
import { Plus, Trash2, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Drawer } from "@/components/ui/drawer";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";
import type { AgentCard } from "@/lib/mocks/characters";

/**
 * Right-side edit drawer for a Character card — Tidepool retoken.
 *
 * Chrome (slide, overlay, focus-trap, Esc-to-close, close button) still
 * comes from the shared Radix-based `<Drawer>` primitive. Only this
 * component's body was retokened away from the neutral accent-2/muted
 * palette onto the warm Tidepool glass vocabulary
 * (`bg-tp-glass-inner`, `text-tp-ink-*`, `border-tp-glass-edge`,
 * `bg-tp-amber-soft`).
 *
 * MVP scope (unchanged from B2-FE4):
 *   - Name is read-only; creating new agents lands in B2-BE5.
 *   - Description + system prompt are plain textareas. Monaco syntax
 *     highlight ships later; keeping the surface boring makes the token-
 *     highlight preview legible today.
 *   - Variables: simple key/value table with add/remove rows.
 *   - tools_allowed + skill_refs: chips with click-to-remove + a small input
 *     to add new entries.
 *   - Save fires a stub mutation. Wire to `PATCH /admin/agents/:name` in
 *     B2-BE5 — see TODO inside `handleSave`.
 */
export interface CharacterDrawerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** `null` → create mode (empty form). Otherwise we prefill from the card. */
  card: AgentCard | null;
}

interface DraftState {
  name: string;
  description: string;
  systemPrompt: string;
  variables: Array<[string, string]>;
  toolsAllowed: string[];
  skillRefs: string[];
}

function cardToDraft(card: AgentCard | null): DraftState {
  if (!card) {
    return {
      name: "",
      description: "",
      systemPrompt: "",
      variables: [],
      toolsAllowed: [],
      skillRefs: [],
    };
  }
  return {
    name: card.name,
    description: card.description,
    systemPrompt: card.system_prompt,
    variables: Object.entries(card.variables),
    toolsAllowed: [...card.tools_allowed],
    skillRefs: [...card.skill_refs],
  };
}

function draftsEqual(a: DraftState, b: DraftState): boolean {
  if (a.name !== b.name) return false;
  if (a.description !== b.description) return false;
  if (a.systemPrompt !== b.systemPrompt) return false;
  if (a.variables.length !== b.variables.length) return false;
  for (let i = 0; i < a.variables.length; i++) {
    const [ak, av] = a.variables[i]!;
    const [bk, bv] = b.variables[i]!;
    if (ak !== bk || av !== bv) return false;
  }
  if (a.toolsAllowed.join("|") !== b.toolsAllowed.join("|")) return false;
  if (a.skillRefs.join("|") !== b.skillRefs.join("|")) return false;
  return true;
}

/** Parse `{{agent.name}}` / `{{anything.like.this}}` tokens out of a prompt. */
function parseTokens(text: string): string[] {
  const tokens: string[] = [];
  const re = /\{\{\s*([^}]+?)\s*\}\}/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    tokens.push(m[1]!);
  }
  return tokens;
}

export function CharacterDrawer({ open, onOpenChange, card }: CharacterDrawerProps) {
  const initial = React.useMemo(() => cardToDraft(card), [card]);
  const [draft, setDraft] = React.useState<DraftState>(initial);

  // Reset the draft whenever the drawer is opened on a new card.
  React.useEffect(() => {
    if (open) setDraft(initial);
  }, [open, initial]);

  const dirty = !draftsEqual(draft, initial);
  const tokens = React.useMemo(
    () => parseTokens(draft.systemPrompt),
    [draft.systemPrompt],
  );

  const [toolDraft, setToolDraft] = React.useState("");
  const [skillDraft, setSkillDraft] = React.useState("");

  function handleSave() {
    // TODO(B2-BE5): wire to PATCH /admin/agents/:name
    onOpenChange(false);
  }

  function addVariable() {
    setDraft((d) => ({ ...d, variables: [...d.variables, ["", ""]] }));
  }

  function updateVariable(idx: number, next: [string, string]) {
    setDraft((d) => {
      const copy = [...d.variables];
      copy[idx] = next;
      return { ...d, variables: copy };
    });
  }

  function removeVariable(idx: number) {
    setDraft((d) => ({ ...d, variables: d.variables.filter((_, i) => i !== idx) }));
  }

  function addChip(field: "toolsAllowed" | "skillRefs", value: string) {
    const trimmed = value.trim();
    if (!trimmed) return;
    setDraft((d) => {
      if (d[field].includes(trimmed)) return d;
      return { ...d, [field]: [...d[field], trimmed] };
    });
  }

  function removeChip(field: "toolsAllowed" | "skillRefs", value: string) {
    setDraft((d) => ({ ...d, [field]: d[field].filter((v) => v !== value) }));
  }

  const title = card ? card.name : "New character";
  const description = card
    ? "Tune the prompt, tools and variables for this character."
    : "Scaffold a new character card. Saving is wired up in B2-BE5.";

  return (
    <Drawer
      open={open}
      onOpenChange={onOpenChange}
      width="lg"
      title={title}
      description={description}
      className="gap-0"
      footer={
        <>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            onClick={handleSave}
            disabled={!dirty}
            data-testid="character-drawer-save"
          >
            Save
          </Button>
        </>
      }
    >
      <div className="px-6 py-5" data-testid="character-drawer">
        <div className="space-y-5">
          <Field
            label="Name"
            htmlFor="char-name"
            hint="Read-only for MVP — new names come in B2-BE5."
          >
            <Input
              id="char-name"
              value={draft.name}
              readOnly
              disabled
              className="font-mono text-xs"
            />
          </Field>

          <Field label="Description" htmlFor="char-desc">
            <textarea
              id="char-desc"
              value={draft.description}
              onChange={(e) =>
                setDraft((d) => ({ ...d, description: e.target.value }))
              }
              rows={2}
              className={cn(
                "flex w-full rounded-md border px-3 py-2 text-sm shadow-sm",
                "border-tp-glass-edge bg-tp-glass-inner text-tp-ink",
                "placeholder:text-tp-ink-4",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/50",
              )}
            />
          </Field>

          <Field
            label="System prompt"
            htmlFor="char-prompt"
            hint="Use {{agent.name}} to reference other characters."
          >
            <textarea
              id="char-prompt"
              value={draft.systemPrompt}
              onChange={(e) =>
                setDraft((d) => ({ ...d, systemPrompt: e.target.value }))
              }
              rows={8}
              className={cn(
                "flex w-full rounded-md border px-3 py-2 font-mono text-xs leading-relaxed shadow-sm",
                "border-tp-glass-edge bg-tp-glass-inner text-tp-ink-2",
                "placeholder:text-tp-ink-4",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/50",
              )}
            />
            <TokenPreview tokens={tokens} />
          </Field>

          <Field label="Variables">
            <div className="space-y-2">
              {draft.variables.length === 0 ? (
                <p className="text-xs italic text-tp-ink-4">
                  No variables yet.
                </p>
              ) : null}
              {draft.variables.map(([k, v], idx) => (
                <div key={idx} className="flex items-center gap-2">
                  <Input
                    value={k}
                    placeholder="key"
                    onChange={(e) => updateVariable(idx, [e.target.value, v])}
                    className="h-8 w-40 font-mono text-xs"
                  />
                  <span className="text-xs text-tp-ink-3">=</span>
                  <Input
                    value={v}
                    placeholder="value"
                    onChange={(e) => updateVariable(idx, [k, e.target.value])}
                    className="h-8 flex-1 font-mono text-xs"
                  />
                  <button
                    type="button"
                    onClick={() => removeVariable(idx)}
                    aria-label={`Remove variable ${k || "row"}`}
                    className={cn(
                      "inline-flex h-8 w-8 items-center justify-center rounded-md",
                      "text-tp-ink-3 transition-colors",
                      "hover:bg-tp-glass-inner-hover hover:text-tp-ink",
                      "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/40",
                    )}
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                </div>
              ))}
              <Button
                type="button"
                size="sm"
                variant="outline"
                onClick={addVariable}
              >
                <Plus className="h-3 w-3" />
                Add variable
              </Button>
            </div>
          </Field>

          <Field label="Tools allowed">
            <ChipEditor
              items={draft.toolsAllowed}
              draft={toolDraft}
              setDraft={setToolDraft}
              onAdd={(v) => addChip("toolsAllowed", v)}
              onRemove={(v) => removeChip("toolsAllowed", v)}
              placeholder="read_file, run_tests…"
              testIdPrefix="char-tool"
            />
          </Field>

          <Field label="Skill refs">
            <ChipEditor
              items={draft.skillRefs}
              draft={skillDraft}
              setDraft={setSkillDraft}
              onAdd={(v) => addChip("skillRefs", v)}
              onRemove={(v) => removeChip("skillRefs", v)}
              placeholder="test-driven-development…"
              testIdPrefix="char-skill"
            />
          </Field>
        </div>
      </div>
    </Drawer>
  );
}

// --- field helpers ---------------------------------------------------------

function Field({
  label,
  htmlFor,
  hint,
  children,
}: {
  label: string;
  htmlFor?: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-1.5">
      <div className="flex items-baseline justify-between gap-2">
        <Label htmlFor={htmlFor} className="text-xs font-medium text-tp-ink">
          {label}
        </Label>
        {hint ? (
          <span className="text-[10px] text-tp-ink-4">{hint}</span>
        ) : null}
      </div>
      {children}
    </div>
  );
}

function TokenPreview({ tokens }: { tokens: string[] }) {
  if (tokens.length === 0) return null;
  return (
    <div className="mt-1.5 space-y-1">
      <div className="font-mono text-[10px] uppercase tracking-[0.1em] text-tp-ink-4">
        tokens
      </div>
      <pre
        aria-label="Parsed tokens in the system prompt"
        className={cn(
          "flex flex-wrap gap-1.5 rounded-md border border-dashed p-2 font-mono text-[10px]",
          "border-tp-glass-edge bg-tp-glass-inner/60",
        )}
      >
        {tokens.map((t, i) => (
          <span
            key={`${t}-${i}`}
            className={cn(
              "inline-flex items-center rounded-full px-2 py-0.5",
              "bg-tp-amber-soft text-tp-amber border border-tp-amber/25",
            )}
            data-testid={`char-token-${i}`}
          >
            {t}
          </span>
        ))}
      </pre>
    </div>
  );
}

function ChipEditor({
  items,
  draft,
  setDraft,
  onAdd,
  onRemove,
  placeholder,
  testIdPrefix,
}: {
  items: string[];
  draft: string;
  setDraft: (v: string) => void;
  onAdd: (v: string) => void;
  onRemove: (v: string) => void;
  placeholder: string;
  testIdPrefix: string;
}) {
  return (
    <div className="space-y-2">
      <ul className="flex flex-wrap gap-1.5">
        {items.length === 0 ? (
          <li className="text-xs italic text-tp-ink-4">None yet.</li>
        ) : (
          items.map((item) => (
            <li
              key={item}
              className={cn(
                "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 font-mono text-[10px]",
                "border-tp-glass-edge bg-tp-glass-inner text-tp-ink-2",
              )}
              data-testid={`${testIdPrefix}-chip-${item}`}
            >
              {item}
              <button
                type="button"
                onClick={() => onRemove(item)}
                aria-label={`Remove ${item}`}
                className={cn(
                  "text-tp-ink-4 transition-colors",
                  "hover:text-tp-ink",
                  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/40 rounded",
                )}
              >
                <X className="h-3 w-3" />
              </button>
            </li>
          ))
        )}
      </ul>
      <div className="flex items-center gap-2">
        <Input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder={placeholder}
          className="h-8 flex-1 font-mono text-xs"
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              onAdd(draft);
              setDraft("");
            }
          }}
        />
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={() => {
            onAdd(draft);
            setDraft("");
          }}
        >
          <Plus className="h-3 w-3" />
          Add
        </Button>
      </div>
    </div>
  );
}
