"use client";

/**
 * First-run onboarding page — 4-step wizard.
 *
 * Steps:
 *   1. account   — username + password (POST /admin/onboard)
 *   2. newapi    — base_url + token + admin_token
 *                  (POST /admin/onboard/newapi/probe)
 *   3. models    — pick LLM / embedding / TTS defaults from
 *                  the live channel list
 *                  (POST /admin/onboard/newapi/channels)
 *   4. confirm   — atomic config write
 *                  (POST /admin/onboard/finalize) → /login
 *
 * State lives in this component (React useState). No server-side
 * session — each backend call carries everything inline. The user
 * types the newapi token twice (once at probe, once at finalize);
 * trade-off accepted to avoid DashMap/cookie wiring.
 */

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useTranslation } from "react-i18next";

import { onboard } from "@/lib/auth";
import {
  CorlinmanApiError,
  finalizeOnboard,
  listOnboardChannels,
  probeNewapi,
  type NewapiChannel,
} from "@/lib/api";
import { BrandMark } from "@/components/layout/brand-mark";
import { LanguageToggle } from "@/components/layout/language-toggle";
import { ThemeToggle } from "@/components/layout/theme-toggle";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

const MIN_PASSWORD_LEN = 8;

type Step = "account" | "newapi" | "models" | "confirm";

interface NewapiState {
  base_url: string;
  token: string;
  admin_token: string;
}

interface ModelPickState {
  channel_id?: number;
  model: string;
}

interface TtsPickState extends ModelPickState {
  voice?: string;
}

export default function OnboardPage() {
  return (
    <div className="relative grid min-h-dvh grid-cols-1 bg-background md:grid-cols-[40%_60%]">
      <div className="absolute right-4 top-4 z-10 flex items-center gap-2">
        <LanguageToggle />
        <ThemeToggle />
      </div>
      <HeroColumn />
      <div className="flex items-center justify-center p-8">
        <OnboardWizard />
      </div>
    </div>
  );
}

function HeroColumn() {
  const { t } = useTranslation();
  return (
    <aside className="relative hidden overflow-hidden border-r border-tp-glass-edge bg-tp-glass-inner md:flex md:flex-col md:justify-between md:p-10">
      <div className="flex items-center gap-2">
        <BrandMark />
      </div>
      <div className="relative z-10 space-y-2">
        <h2 className="text-lg font-semibold tracking-tight">
          {t("auth.onboardHeroTitle")}
        </h2>
        <p className="max-w-xs text-sm text-tp-ink-3">
          {t("auth.onboardHeroBody")}
        </p>
      </div>
      <div className="flex items-center gap-2 text-xs text-tp-ink-3">
        <span className="font-mono">v0.5.0-newapi</span>
        <span>·</span>
        <span>first-run</span>
      </div>
      <div
        className="pointer-events-none absolute inset-0 dot-grid opacity-60"
        aria-hidden
      />
      <div
        className="pointer-events-none absolute inset-0 bg-[radial-gradient(600px_300px_at_20%_20%,hsl(var(--primary)/0.15),transparent_60%)]"
        aria-hidden
      />
    </aside>
  );
}

function OnboardWizard() {
  const { t } = useTranslation();
  const [step, setStep] = useState<Step>("account");
  const [newapi, setNewapi] = useState<NewapiState>({
    base_url: "",
    token: "",
    admin_token: "",
  });
  const [llmPick, setLlmPick] = useState<ModelPickState | null>(null);
  const [embPick, setEmbPick] = useState<ModelPickState | null>(null);
  const [ttsPick, setTtsPick] = useState<TtsPickState | null>(null);

  return (
    <div className="w-full max-w-md space-y-6">
      <div className="space-y-1.5 md:hidden">
        <BrandMark />
      </div>
      <StepIndicator current={step} />
      {step === "account" && <AccountStep onDone={() => setStep("newapi")} />}
      {step === "newapi" && (
        <NewapiStep
          value={newapi}
          onChange={setNewapi}
          onBack={() => setStep("account")}
          onDone={() => setStep("models")}
        />
      )}
      {step === "models" && (
        <ModelsStep
          newapi={newapi}
          llmPick={llmPick}
          embPick={embPick}
          ttsPick={ttsPick}
          onLlmChange={setLlmPick}
          onEmbChange={setEmbPick}
          onTtsChange={setTtsPick}
          onBack={() => setStep("newapi")}
          onDone={() => setStep("confirm")}
        />
      )}
      {step === "confirm" && (
        <ConfirmStep
          newapi={newapi}
          llmPick={llmPick}
          embPick={embPick}
          ttsPick={ttsPick}
          onBack={() => setStep("models")}
        />
      )}
      <p className="text-center text-xs text-tp-ink-3">
        {t("auth.onboardHint")}
      </p>
    </div>
  );
}

function StepIndicator({ current }: { current: Step }) {
  const { t } = useTranslation();
  const steps: { key: Step; label: string }[] = [
    { key: "account", label: t("auth.onboardStepAccount") },
    { key: "newapi", label: t("auth.onboardStepNewapi") },
    { key: "models", label: t("auth.onboardStepModels") },
    { key: "confirm", label: t("auth.onboardStepConfirm") },
  ];
  const idx = steps.findIndex((s) => s.key === current);
  return (
    <ol className="flex items-center gap-2 text-xs">
      {steps.map((s, i) => (
        <li key={s.key} className="flex items-center gap-2">
          <span
            className={`inline-flex h-6 w-6 items-center justify-center rounded-full border ${
              i <= idx
                ? "border-primary bg-primary text-primary-foreground"
                : "border-tp-glass-edge text-tp-ink-3"
            }`}
          >
            {i + 1}
          </span>
          <span
            className={
              i === idx ? "font-medium" : "text-tp-ink-3 hidden md:inline"
            }
          >
            {s.label}
          </span>
          {i < steps.length - 1 && (
            <span aria-hidden className="mx-1 text-tp-ink-3">
              →
            </span>
          )}
        </li>
      ))}
    </ol>
  );
}

// ---------------------------------------------------------------------------
// Step 1: Account
// ---------------------------------------------------------------------------

function AccountStep({ onDone }: { onDone: () => void }) {
  const { t } = useTranslation();
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setError(null);
    if (password.length < MIN_PASSWORD_LEN) {
      setError(t("auth.onboardWeakPassword", { min: MIN_PASSWORD_LEN }));
      return;
    }
    if (password !== confirm) {
      setError(t("auth.onboardPasswordMismatch"));
      return;
    }
    setSubmitting(true);
    try {
      await onboard({ username, password });
      onDone();
    } catch (err) {
      if (err instanceof CorlinmanApiError) {
        if (err.status === 409) {
          setError(t("auth.onboardAlreadyConfigured"));
          setTimeout(() => router.replace("/login"), 1500);
        } else if (err.status === 422) {
          setError(t("auth.onboardWeakPassword", { min: MIN_PASSWORD_LEN }));
        } else {
          setError(err.message);
        }
      } else {
        setError(String(err));
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <>
      <div className="space-y-1">
        <h1 className="text-xl font-semibold tracking-tight">
          {t("auth.onboardTitle")}
        </h1>
        <p className="text-sm text-tp-ink-3">{t("auth.onboardSubtitle")}</p>
      </div>
      <form onSubmit={onSubmit} className="space-y-4">
        <div className="space-y-2">
          <Label htmlFor="username">{t("auth.username")}</Label>
          <Input
            id="username"
            name="username"
            autoComplete="username"
            required
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            disabled={submitting}
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor="password">{t("auth.password")}</Label>
          <Input
            id="password"
            name="password"
            type="password"
            autoComplete="new-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            disabled={submitting}
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor="confirm">{t("auth.onboardConfirmPassword")}</Label>
          <Input
            id="confirm"
            name="confirm"
            type="password"
            autoComplete="new-password"
            required
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            disabled={submitting}
          />
        </div>
        {error ? (
          <p
            role="alert"
            className="text-sm text-destructive"
            data-testid="onboard-error"
          >
            {error}
          </p>
        ) : null}
        <Button type="submit" className="w-full" disabled={submitting}>
          {submitting ? t("auth.submitting") : t("auth.onboardSubmit")}
        </Button>
      </form>
    </>
  );
}

// ---------------------------------------------------------------------------
// Step 2: newapi connect
// ---------------------------------------------------------------------------

function NewapiStep({
  value,
  onChange,
  onBack,
  onDone,
}: {
  value: NewapiState;
  onChange: (v: NewapiState) => void;
  onBack: () => void;
  onDone: () => void;
}) {
  const { t } = useTranslation();
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await probeNewapi({
        base_url: value.base_url,
        token: value.token,
        admin_token: value.admin_token || undefined,
      });
      onDone();
    } catch (err) {
      if (err instanceof CorlinmanApiError) {
        const code = err.message || "newapi_probe_failed";
        const i18nKey = `auth.error${toCamel(code)}`;
        setError(t(i18nKey, code));
      } else {
        setError(String(err));
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <>
      <div className="space-y-1">
        <h1 className="text-xl font-semibold tracking-tight">
          {t("auth.onboardNewapiTitle")}
        </h1>
        <p className="text-sm text-tp-ink-3">
          {t("auth.onboardNewapiSubtitle")}
        </p>
      </div>
      <form onSubmit={onSubmit} className="space-y-4">
        <div className="space-y-2">
          <Label htmlFor="base_url">{t("auth.onboardNewapiBaseUrl")}</Label>
          <Input
            id="base_url"
            type="url"
            required
            placeholder="http://localhost:3000"
            value={value.base_url}
            onChange={(e) => onChange({ ...value, base_url: e.target.value })}
            disabled={submitting}
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor="token">{t("auth.onboardNewapiToken")}</Label>
          <Input
            id="token"
            type="password"
            required
            value={value.token}
            onChange={(e) => onChange({ ...value, token: e.target.value })}
            disabled={submitting}
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor="admin_token">
            {t("auth.onboardNewapiAdminToken")}
          </Label>
          <Input
            id="admin_token"
            type="password"
            value={value.admin_token}
            onChange={(e) =>
              onChange({ ...value, admin_token: e.target.value })
            }
            disabled={submitting}
          />
          <p className="text-xs text-tp-ink-3">
            {t("auth.onboardNewapiAdminTokenHint")}
          </p>
        </div>
        {error ? (
          <p
            role="alert"
            className="text-sm text-destructive"
            data-testid="onboard-error"
          >
            {error}
          </p>
        ) : null}
        <div className="flex gap-2">
          <Button type="button" variant="outline" onClick={onBack}>
            {t("auth.onboardBack")}
          </Button>
          <Button type="submit" className="flex-1" disabled={submitting}>
            {submitting ? t("auth.submitting") : t("auth.onboardNext")}
          </Button>
        </div>
      </form>
    </>
  );
}

// ---------------------------------------------------------------------------
// Step 3: Pick models
// ---------------------------------------------------------------------------

function ModelsStep({
  newapi,
  llmPick,
  embPick,
  ttsPick,
  onLlmChange,
  onEmbChange,
  onTtsChange,
  onBack,
  onDone,
}: {
  newapi: NewapiState;
  llmPick: ModelPickState | null;
  embPick: ModelPickState | null;
  ttsPick: TtsPickState | null;
  onLlmChange: (v: ModelPickState | null) => void;
  onEmbChange: (v: ModelPickState | null) => void;
  onTtsChange: (v: TtsPickState | null) => void;
  onBack: () => void;
  onDone: () => void;
}) {
  const { t } = useTranslation();
  const [llmChannels, setLlmChannels] = useState<NewapiChannel[]>([]);
  const [embChannels, setEmbChannels] = useState<NewapiChannel[]>([]);
  const [ttsChannels, setTtsChannels] = useState<NewapiChannel[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    const conn = {
      base_url: newapi.base_url,
      token: newapi.token,
      admin_token: newapi.admin_token || undefined,
    };
    async function load() {
      setLoading(true);
      setError(null);
      try {
        const [a, b, c] = await Promise.all([
          listOnboardChannels(conn, "llm").catch(() => ({ channels: [] })),
          listOnboardChannels(conn, "embedding").catch(() => ({
            channels: [],
          })),
          listOnboardChannels(conn, "tts").catch(() => ({ channels: [] })),
        ]);
        if (!active) return;
        setLlmChannels(a.channels);
        setEmbChannels(b.channels);
        setTtsChannels(c.channels);
        if (a.channels.length > 0 && !llmPick) {
          const ch = a.channels[0];
          onLlmChange({
            channel_id: ch.id,
            model: ch.models.split(",")[0].trim(),
          });
        }
        if (b.channels.length > 0 && !embPick) {
          const ch = b.channels[0];
          onEmbChange({
            channel_id: ch.id,
            model: ch.models.split(",")[0].trim(),
          });
        }
        if (c.channels.length > 0 && !ttsPick) {
          const ch = c.channels[0];
          onTtsChange({
            channel_id: ch.id,
            model: ch.models.split(",")[0].trim(),
            voice: "alloy",
          });
        }
      } catch (err) {
        if (!active) return;
        if (err instanceof CorlinmanApiError) {
          setError(t(`auth.error${toCamel(err.message)}`, err.message));
        } else {
          setError(String(err));
        }
      } finally {
        if (active) setLoading(false);
      }
    }
    void load();
    return () => {
      active = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function onSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!llmPick) {
      setError(t("auth.onboardModelsNoLlm"));
      return;
    }
    onDone();
  }

  return (
    <>
      <div className="space-y-1">
        <h1 className="text-xl font-semibold tracking-tight">
          {t("auth.onboardModelsTitle")}
        </h1>
        <p className="text-sm text-tp-ink-3">
          {t("auth.onboardModelsSubtitle")}
        </p>
      </div>
      <form onSubmit={onSubmit} className="space-y-4">
        <ChannelPicker
          label={t("auth.onboardModelsLlm")}
          channels={llmChannels}
          emptyHint={t("auth.onboardModelsNoLlm")}
          value={llmPick}
          onChange={onLlmChange}
          disabled={loading}
        />
        <ChannelPicker
          label={t("auth.onboardModelsEmbedding")}
          channels={embChannels}
          emptyHint={t("auth.onboardModelsNoEmbedding")}
          value={embPick}
          onChange={onEmbChange}
          disabled={loading}
        />
        <ChannelPicker
          label={t("auth.onboardModelsTts")}
          channels={ttsChannels}
          emptyHint={t("auth.onboardModelsNoTts")}
          value={ttsPick}
          onChange={(v) =>
            onTtsChange(
              v ? { channel_id: v.channel_id, model: v.model, voice: "alloy" } : null,
            )
          }
          disabled={loading}
        />
        {error ? (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        ) : null}
        <div className="flex gap-2">
          <Button type="button" variant="outline" onClick={onBack}>
            {t("auth.onboardBack")}
          </Button>
          <Button type="submit" className="flex-1" disabled={loading}>
            {t("auth.onboardNext")}
          </Button>
        </div>
      </form>
    </>
  );
}

function ChannelPicker({
  label,
  channels,
  emptyHint,
  value,
  onChange,
  disabled,
}: {
  label: string;
  channels: NewapiChannel[];
  emptyHint: string;
  value: ModelPickState | null;
  onChange: (v: ModelPickState | null) => void;
  disabled?: boolean;
}) {
  if (channels.length === 0) {
    return (
      <div className="space-y-1">
        <Label>{label}</Label>
        <p className="text-xs text-tp-ink-3">{emptyHint}</p>
      </div>
    );
  }
  const selectedChannel = channels.find((c) => c.id === value?.channel_id);
  const models = selectedChannel
    ? selectedChannel.models.split(",").map((m) => m.trim()).filter(Boolean)
    : [];
  return (
    <div className="space-y-2">
      <Label>{label}</Label>
      <div className="flex gap-2">
        <select
          className="flex-1 rounded-md border bg-background px-2 py-1.5 text-sm"
          value={value?.channel_id ?? channels[0].id}
          disabled={disabled}
          onChange={(e) => {
            const id = Number(e.target.value);
            const ch = channels.find((c) => c.id === id);
            if (!ch) return;
            onChange({
              channel_id: id,
              model: ch.models.split(",")[0].trim(),
            });
          }}
        >
          {channels.map((c) => (
            <option key={c.id} value={c.id}>
              {c.name}
            </option>
          ))}
        </select>
        <select
          className="flex-1 rounded-md border bg-background px-2 py-1.5 text-sm font-mono"
          value={value?.model ?? models[0]}
          disabled={disabled || !selectedChannel}
          onChange={(e) =>
            value &&
            onChange({ channel_id: value.channel_id, model: e.target.value })
          }
        >
          {models.map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
        </select>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Step 4: Confirm
// ---------------------------------------------------------------------------

function ConfirmStep({
  newapi,
  llmPick,
  embPick,
  ttsPick,
  onBack,
}: {
  newapi: NewapiState;
  llmPick: ModelPickState | null;
  embPick: ModelPickState | null;
  ttsPick: TtsPickState | null;
  onBack: () => void;
}) {
  const { t } = useTranslation();
  const router = useRouter();
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onConfirm() {
    if (!llmPick) {
      setError(t("auth.onboardModelsNoLlm"));
      return;
    }
    setError(null);
    setSubmitting(true);
    try {
      await finalizeOnboard({
        base_url: newapi.base_url,
        token: newapi.token,
        admin_token: newapi.admin_token || undefined,
        llm: { channel_id: llmPick.channel_id, model: llmPick.model },
        embedding: embPick
          ? {
              channel_id: embPick.channel_id,
              model: embPick.model,
              dimension: 1536,
            }
          : { model: "text-embedding-3-small", dimension: 1536 },
        tts: ttsPick
          ? {
              channel_id: ttsPick.channel_id,
              model: ttsPick.model,
              voice: ttsPick.voice,
            }
          : undefined,
      });
      router.replace("/login");
    } catch (err) {
      if (err instanceof CorlinmanApiError) {
        setError(
          t("auth.onboardFinalizeError", { detail: err.message }),
        );
      } else {
        setError(String(err));
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <>
      <div className="space-y-1">
        <h1 className="text-xl font-semibold tracking-tight">
          {t("auth.onboardConfirmTitle")}
        </h1>
        <p className="text-sm text-tp-ink-3">{t("auth.onboardConfirmBody")}</p>
      </div>
      <dl className="rounded-md border bg-tp-glass-inner p-3 text-sm">
        <dt className="text-tp-ink-3">{t("auth.onboardNewapiBaseUrl")}</dt>
        <dd className="font-mono">{newapi.base_url}</dd>
        <dt className="mt-2 text-tp-ink-3">
          {t("auth.onboardModelsLlm")}
        </dt>
        <dd className="font-mono">{llmPick?.model ?? "—"}</dd>
        <dt className="mt-2 text-tp-ink-3">
          {t("auth.onboardModelsEmbedding")}
        </dt>
        <dd className="font-mono">{embPick?.model ?? "—"}</dd>
        <dt className="mt-2 text-tp-ink-3">
          {t("auth.onboardModelsTts")}
        </dt>
        <dd className="font-mono">{ttsPick?.model ?? "—"}</dd>
      </dl>
      {error ? (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      ) : null}
      <div className="flex gap-2">
        <Button type="button" variant="outline" onClick={onBack}>
          {t("auth.onboardBack")}
        </Button>
        <Button
          type="button"
          className="flex-1"
          disabled={submitting || !llmPick}
          onClick={onConfirm}
        >
          {submitting ? t("auth.submitting") : t("auth.onboardFinish")}
        </Button>
      </div>
    </>
  );
}

// Lowercase-snake → CamelCase helper for error code → i18n key mapping.
function toCamel(code: string): string {
  return code
    .split("_")
    .map((p) => (p.length === 0 ? "" : p[0].toUpperCase() + p.slice(1)))
    .join("");
}
