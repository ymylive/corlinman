"use client";

/**
 * Hand-rolled JSON Schema (draft 2020-12) renderer.
 *
 * Supported constructs:
 *   - type: "string" | "number" | "integer" | "boolean" | "object"
 *   - enum → select
 *   - number/integer with `minimum` + `maximum` → range slider + number input
 *   - string with `maxLength > 200` or `format: "prompt"` → textarea
 *   - boolean → switch
 *   - nested object → fieldset (labelled by `title` / description)
 *
 * Deliberately not a full validator — we only cover the subset needed to
 * render provider + embedding params forms today. Heavier schemas (ajv,
 * react-hook-form-schema) are explicitly out of scope per Feature C contract.
 *
 * Validation runs on blur + on submit. Errors render inline beneath the
 * control; the parent reads `errors` via the `onErrorsChange` callback so
 * the save button can stay disabled while the form is dirty.
 */

import * as React from "react";
import { useTranslation } from "react-i18next";

import type { JSONSchema } from "@/lib/api";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";

type ParamsValue = Record<string, unknown>;

export interface DynamicParamsFormProps {
  schema: JSONSchema | null | undefined;
  value: ParamsValue;
  onChange: (next: ParamsValue) => void;
  /** Read-only view. No inputs accept focus. */
  disabled?: boolean;
  /** Optional bubble-up of the validation errors keyed by JSON pointer. */
  onErrorsChange?: (errors: Record<string, string>) => void;
  /** Prefix for `data-testid` so parent pages can scope multiple forms. */
  testIdPrefix?: string;
}

/** Walks the schema, validates `value`, returns errors keyed by json-pointer. */
export function validateAgainstSchema(
  schema: JSONSchema | null | undefined,
  value: unknown,
  pointer = "",
): Record<string, string> {
  const errors: Record<string, string> = {};
  if (!schema) return errors;

  const type = schema.type;
  if (value === undefined || value === null) {
    // Required handling sits on the parent object.
    return errors;
  }

  if (type === "string" && typeof value !== "string") {
    errors[pointer] = "expected string";
    return errors;
  }
  if ((type === "number" || type === "integer") && typeof value !== "number") {
    errors[pointer] = "expected number";
    return errors;
  }
  if (type === "boolean" && typeof value !== "boolean") {
    errors[pointer] = "expected boolean";
    return errors;
  }

  if (type === "number" || type === "integer") {
    const n = value as number;
    if (typeof schema.minimum === "number" && n < schema.minimum) {
      errors[pointer] = `≥ ${schema.minimum}`;
    }
    if (typeof schema.maximum === "number" && n > schema.maximum) {
      errors[pointer] = `≤ ${schema.maximum}`;
    }
    if (type === "integer" && !Number.isInteger(n)) {
      errors[pointer] = "must be an integer";
    }
  }

  if (type === "string") {
    const s = value as string;
    if (typeof schema.maxLength === "number" && s.length > schema.maxLength) {
      errors[pointer] = `≤ ${schema.maxLength} chars`;
    }
    if (typeof schema.minLength === "number" && s.length < schema.minLength) {
      errors[pointer] = `≥ ${schema.minLength} chars`;
    }
  }

  if (Array.isArray(schema.enum) && !schema.enum.includes(value)) {
    errors[pointer] = "not in allowed values";
  }

  if (type === "object" && schema.properties) {
    const obj =
      typeof value === "object" && value !== null && !Array.isArray(value)
        ? (value as ParamsValue)
        : ({} as ParamsValue);
    for (const [key, sub] of Object.entries(schema.properties)) {
      const subPointer = `${pointer}/${key}`;
      if (
        schema.required?.includes(key) &&
        (obj[key] === undefined || obj[key] === null || obj[key] === "")
      ) {
        errors[subPointer] = "required";
        continue;
      }
      Object.assign(
        errors,
        validateAgainstSchema(sub, obj[key], subPointer),
      );
    }
  }

  return errors;
}

export function DynamicParamsForm({
  schema,
  value,
  onChange,
  disabled,
  onErrorsChange,
  testIdPrefix = "params",
}: DynamicParamsFormProps) {
  const { t } = useTranslation();
  const [touched, setTouched] = React.useState<Set<string>>(new Set());

  // Schemas without a declared `properties` map render as a noop — the
  // backend will fall back to defaults.
  const properties = React.useMemo(
    () => schema?.properties ?? {},
    [schema],
  );
  const propertyKeys = React.useMemo(
    () => Object.keys(properties),
    [properties],
  );

  const errors = React.useMemo(
    () => validateAgainstSchema(schema ?? null, value),
    [schema, value],
  );

  // Report errors upward. Skipping this effect when the errors object is
  // shallow-equal keeps React Query + form-state in sync without tight loops.
  const errorsKey = React.useMemo(
    () => JSON.stringify(errors),
    [errors],
  );
  React.useEffect(() => {
    onErrorsChange?.(errors);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [errorsKey]);

  const markTouched = (pointer: string) => {
    setTouched((prev) => {
      if (prev.has(pointer)) return prev;
      const next = new Set(prev);
      next.add(pointer);
      return next;
    });
  };

  if (propertyKeys.length === 0) {
    return (
      <p className="text-xs italic text-muted-foreground">
        {t("common.none")}
      </p>
    );
  }

  const setField = (key: string, v: unknown) => {
    onChange({ ...value, [key]: v });
  };

  return (
    <div
      className="space-y-3"
      data-testid={`${testIdPrefix}-form`}
    >
      {propertyKeys.map((key) => {
        const fieldSchema = properties[key] ?? {};
        const pointer = `/${key}`;
        const errMsg = touched.has(pointer) ? errors[pointer] : undefined;
        return (
          <SchemaField
            key={key}
            name={key}
            pointer={pointer}
            schema={fieldSchema}
            value={value?.[key]}
            disabled={disabled}
            error={errMsg}
            onBlur={() => markTouched(pointer)}
            onChange={(v) => setField(key, v)}
            testIdPrefix={testIdPrefix}
          />
        );
      })}
    </div>
  );
}

interface SchemaFieldProps {
  name: string;
  pointer: string;
  schema: JSONSchema;
  value: unknown;
  disabled?: boolean;
  error?: string;
  onBlur: () => void;
  onChange: (v: unknown) => void;
  testIdPrefix: string;
}

function SchemaField({
  name,
  pointer,
  schema,
  value,
  disabled,
  error,
  onBlur,
  onChange,
  testIdPrefix,
}: SchemaFieldProps) {
  // Defensive: backend is expected to send a concrete sub-schema for every
  // property, but guard against a misshapen payload so a single bad entry
  // doesn't crash the whole form.
  if (!schema || typeof schema !== "object") {
    return (
      <p className="text-xs text-muted-foreground">(missing schema for {name})</p>
    );
  }

  const labelText = schema.title ?? name;
  const description = schema.description;
  const fieldId = `${testIdPrefix}${pointer.replace(/\//g, "-")}`;

  const kind = resolveKind(schema);

  // Nested object — render as a fieldset with the same renderer recursing.
  if (kind === "object") {
    const nested =
      typeof value === "object" && value !== null && !Array.isArray(value)
        ? (value as ParamsValue)
        : ({} as ParamsValue);
    return (
      <fieldset className="space-y-2 rounded-md border border-dashed border-border px-3 py-2">
        <legend className="px-1 text-xs font-medium text-foreground">
          {labelText}
        </legend>
        {description ? (
          <p className="text-[11px] text-muted-foreground">{description}</p>
        ) : null}
        {Object.entries(schema?.properties ?? {}).map(
          ([subKey, subSchema]) => (
            <SchemaField
              key={subKey}
              name={subKey}
              pointer={`${pointer}/${subKey}`}
              schema={subSchema}
              value={nested[subKey]}
              disabled={disabled}
              onBlur={onBlur}
              onChange={(v) =>
                onChange({ ...nested, [subKey]: v } as ParamsValue)
              }
              testIdPrefix={testIdPrefix}
            />
          ),
        )}
      </fieldset>
    );
  }

  return (
    <div className="space-y-1.5">
      <div className="flex items-baseline justify-between gap-2">
        <Label htmlFor={fieldId} className="text-xs">
          {labelText}
        </Label>
        {error ? (
          <span className="text-[11px] text-destructive">{error}</span>
        ) : null}
      </div>

      {description ? (
        <p className="text-[11px] text-muted-foreground">{description}</p>
      ) : null}

      <ControlForKind
        kind={kind}
        id={fieldId}
        schema={schema}
        value={value}
        disabled={disabled}
        onBlur={onBlur}
        onChange={onChange}
        testId={fieldId}
      />
    </div>
  );
}

type FieldKind =
  | "enum"
  | "boolean"
  | "textarea"
  | "slider"
  | "number"
  | "string"
  | "object"
  | "unsupported";

function resolveKind(schema: JSONSchema): FieldKind {
  if (Array.isArray(schema.enum) && schema.enum.length > 0) return "enum";
  if (schema.type === "boolean") return "boolean";
  if (schema.type === "object") return "object";
  if (schema.type === "string") {
    const isLong =
      (typeof schema.maxLength === "number" && schema.maxLength > 200) ||
      schema.format === "prompt";
    return isLong ? "textarea" : "string";
  }
  if (schema.type === "number" || schema.type === "integer") {
    const hasRange =
      typeof schema.minimum === "number" && typeof schema.maximum === "number";
    return hasRange ? "slider" : "number";
  }
  return "unsupported";
}

interface ControlProps {
  kind: FieldKind;
  id: string;
  schema: JSONSchema;
  value: unknown;
  disabled?: boolean;
  onBlur: () => void;
  onChange: (v: unknown) => void;
  testId: string;
}

function ControlForKind({
  kind,
  id,
  schema,
  value,
  disabled,
  onBlur,
  onChange,
  testId,
}: ControlProps) {
  switch (kind) {
    case "enum": {
      const options = schema.enum ?? [];
      return (
        <select
          id={id}
          data-testid={testId}
          disabled={disabled}
          onBlur={onBlur}
          value={
            typeof value === "string" || typeof value === "number"
              ? String(value)
              : ""
          }
          onChange={(e) => {
            const raw = e.target.value;
            // Preserve number enums as numbers.
            const coerced = options.find((o) => String(o) === raw) ?? raw;
            onChange(coerced);
          }}
          className="flex h-9 w-full items-center rounded-md border border-input bg-transparent px-2 text-sm"
        >
          <option value="" disabled>
            —
          </option>
          {options.map((o) => (
            <option key={String(o)} value={String(o)}>
              {String(o)}
            </option>
          ))}
        </select>
      );
    }

    case "boolean": {
      const checked = value === true;
      return (
        <button
          id={id}
          type="button"
          role="switch"
          aria-checked={checked}
          data-testid={testId}
          disabled={disabled}
          onBlur={onBlur}
          onClick={() => onChange(!checked)}
          className={cn(
            "inline-flex h-6 w-11 shrink-0 items-center rounded-full border border-input transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
            checked ? "bg-primary" : "bg-muted",
          )}
        >
          <span
            className={cn(
              "inline-block h-4 w-4 transform rounded-full bg-background shadow transition-transform",
              checked ? "translate-x-[22px]" : "translate-x-[3px]",
            )}
          />
        </button>
      );
    }

    case "textarea": {
      const s = typeof value === "string" ? value : "";
      return (
        <textarea
          id={id}
          data-testid={testId}
          disabled={disabled}
          onBlur={onBlur}
          value={s}
          onChange={(e) => onChange(e.target.value)}
          className="flex min-h-[80px] w-full rounded-md border border-input bg-transparent px-3 py-2 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
        />
      );
    }

    case "slider": {
      const min = schema.minimum ?? 0;
      const max = schema.maximum ?? 1;
      const step = schema.type === "integer" ? 1 : inferStep(min, max);
      const n = typeof value === "number" ? value : min;
      return (
        <div className="flex items-center gap-3">
          <input
            id={id}
            type="range"
            data-testid={`${testId}-range`}
            disabled={disabled}
            onBlur={onBlur}
            min={min}
            max={max}
            step={step}
            value={n}
            onChange={(e) => onChange(parseFloatOrInt(e.target.value, schema))}
            className="h-1 flex-1 cursor-pointer appearance-none rounded-full bg-muted accent-primary"
          />
          <Input
            data-testid={`${testId}-num`}
            type="number"
            disabled={disabled}
            onBlur={onBlur}
            value={Number.isFinite(n) ? n : ""}
            min={min}
            max={max}
            step={step}
            onChange={(e) => onChange(parseFloatOrInt(e.target.value, schema))}
            className="h-8 w-24 font-mono text-xs"
          />
        </div>
      );
    }

    case "number": {
      const n = typeof value === "number" ? value : "";
      return (
        <Input
          id={id}
          data-testid={testId}
          type="number"
          disabled={disabled}
          onBlur={onBlur}
          value={n}
          onChange={(e) => onChange(parseFloatOrInt(e.target.value, schema))}
          className="h-9"
        />
      );
    }

    case "string": {
      const s = typeof value === "string" ? value : "";
      return (
        <Input
          id={id}
          data-testid={testId}
          type="text"
          disabled={disabled}
          onBlur={onBlur}
          value={s}
          onChange={(e) => onChange(e.target.value)}
          className="h-9"
        />
      );
    }

    default:
      return (
        <p className="text-xs text-muted-foreground">
          (unsupported field type)
        </p>
      );
  }
}

function parseFloatOrInt(raw: string, schema: JSONSchema): number | undefined {
  if (raw === "") return undefined;
  const n = schema.type === "integer" ? parseInt(raw, 10) : parseFloat(raw);
  return Number.isFinite(n) ? n : undefined;
}

function inferStep(min: number, max: number): number {
  const range = Math.abs(max - min);
  if (range <= 1) return 0.01;
  if (range <= 10) return 0.1;
  return 1;
}
