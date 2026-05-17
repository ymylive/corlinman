"use client";

/**
 * First-run onboarding page — 4-step wizard.
 *
 * Wave 2.1 reshape:
 *   - On mount we fetch `/admin/me`. If the gateway already seeded an
 *     admin (401 absent, `must_change_password` returned) we skip Step 1
 *     and land on Step 2 with a small "Using default admin/root" hint
 *     + a "Customize admin account" escape hatch.
 *   - Step 2 ("Connect LLM") adds a `Skip — use mock provider` button
 *     that POSTs `/admin/onboard/finalize-skip` and jumps straight to a
 *     success view (bypassing Step 3 entirely).
 *   - Step 3 replaces the dual `<select>` channel pickers with the
 *     hermes two-stage `ModelPickerDialog` (provider list → model list).
 *   - Step 4 becomes an in-page success card with a primary CTA to
 *     `/account/security` so the operator changes the default password
 *     before going anywhere else.
 *
 * Steps:
 *   1. account   — username + password (POST /admin/onboard)
 *   2. newapi    — base_url + token + admin_token
 *                  (POST /admin/onboard/newapi/probe)  or  skip → mock
 *   3. models    — pick LLM / embedding / TTS via ModelPickerDialog
 *                  (POST /admin/onboard/newapi/channels)
 *   4. confirm   — atomic config write (POST /admin/onboard/finalize)
 *                  then success card → /account/security or /
 *
 * State lives in this component (React useState). No server-side
 * session — each backend call carries everything inline. The user
 * types the newapi token twice (once at probe, once at finalize);
 * trade-off accepted to avoid DashMap/cookie wiring.
 */

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useTranslation } from "react-i18next";
import { CheckCircle2, ChevronRight } from "lucide-react";

import { getSession, onboard } from "@/lib/auth";
import {
  CorlinmanApiError,
  finalizeOnboard,
  finalizeSkipOnboard,
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
import {
  ModelPickerDialog,
  type ModelPickerKind,
} from "@/components/onboard/model-picker-dialog";

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

/** Result of the post-finish flow — drives the success card variant. */
type FinishResult =
  | { kind: "mock" }
  | { kind: "real"; provider: string; model: string };

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

  /**
   * `seededAdmin` = true → /admin/me returned 200 + must_change_password=true.
   * `seededAdmin` = false → we have admin info but it's already customized.
   * `seededAdmin` = null → /admin/me returned 401 (no admin yet); we start
   *                       at the classic account step.
   */
  const [seededAdmin, setSeededAdmin] = useState<boolean | null>(null);
  const [meChecked, setMeChecked] = useState(false);

  /** Set when a finalize succeeds — flips the wizard into success-card mode. */
  const [finished, setFinished] = useState<FinishResult | null>(null);

  // One-shot admin detection. We never re-fetch — the wizard is a single
  // session and the gateway either has admin or it doesn't.
  const probedRef = useRef(false);
  useEffect(() => {
    if (probedRef.current) return;
    probedRef.current = true;
    (async () => {
      try {
        const me = await getSession();
        if (me) {
          const seeded = me.must_change_password === true;
          setSeededAdmin(seeded);
          // Both cases skip Step 1 — only difference is whether we
          // surface the "using default" hint or not.
          setStep("newapi");
        } else {
          setSeededAdmin(null);
        }
      } catch {
        // /admin/me hiccup — fall through to the classic 4-step flow.
        setSeededAdmin(null);
      } finally {
        setMeChecked(true);
      }
    })();
  }, []);

  if (finished) {
    return (
      <div className="w-full max-w-md space-y-6">
        <div className="space-y-1.5 md:hidden">
          <BrandMark />
        </div>
        <SuccessCard result={finished} />
      </div>
    );
  }

  return (
    <div className="w-full max-w-md space-y-6">
      <div className="space-y-1.5 md:hidden">
        <BrandMark />
      </div>
      {seededAdmin === true && step !== "account" ? (
        <div
          className="rounded-md border border-tp-glass-edge bg-tp-glass-inner px-3 py-2 text-xs text-tp-ink-3"
          data-testid="onboard-default-admin-hint"
        >
          {t("auth.onboardUsingDefaultAdmin")}
        </div>
      ) : null}
      <StepIndicator current={step} />
      {seededAdmin !== null && step !== "account" ? (
        <button
          type="button"
          onClick={() => setStep("account")}
          className="text-xs text-tp-ink-3 underline-offset-4 hover:underline"
          data-testid="onboard-customize-admin"
        >
          {t("auth.onboardCustomizeAdmin")}
        </button>
      ) : null}
      {step === "account" && (
        <AccountStep
          // If admin already seeded the wizard treats Step 1 as optional —
          // back into Step 2 if the user changes their mind.
          onSkip={
            seededAdmin !== null ? () => setStep("newapi") : undefined
          }
          onDone={() => setStep("newapi")}
        />
      )}
      {step === "newapi" && (
        <NewapiStep
          value={newapi}
          onChange={setNewapi}
          onBack={() => setStep("account")}
          showBack={seededAdmin === null}
          onDone={() => setStep("models")}
          onSkipMock={(result) => setFinished(result)}
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
          onFinished={(result) => setFinished(result)}
        />
      )}
      <p className="text-center text-xs text-tp-ink-3">
        {t("auth.onboardHint")}
      </p>
      {/* `meChecked` is observed by tests via data attribute; a no-op DOM hook
          keeps it side-effect-free in production. */}
      <span hidden data-testid="onboard-me-checked" data-checked={meChecked} />
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

function AccountStep({
  onDone,
  onSkip,
}: {
  onDone: () => void;
  /** When admin is already seeded, the wizard exposes a back-out link. */
  onSkip?: () => void;
}) {
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
        <div className="flex gap-2">
          {onSkip ? (
            <Button
              type="button"
              variant="outline"
              onClick={onSkip}
              data-testid="account-skip"
            >
              {t("auth.onboardBack")}
            </Button>
          ) : null}
          <Button
            type="submit"
            className="flex-1"
            disabled={submitting}
          >
            {submitting ? t("auth.submitting") : t("auth.onboardSubmit")}
          </Button>
        </div>
      </form>
    </>
  );
}

// ---------------------------------------------------------------------------
// Step 2: newapi connect (with skip → mock provider)
// ---------------------------------------------------------------------------

function NewapiStep({
  value,
  onChange,
  onBack,
  showBack,
  onDone,
  onSkipMock,
}: {
  value: NewapiState;
  onChange: (v: NewapiState) => void;
  onBack: () => void;
  showBack: boolean;
  onDone: () => void;
  onSkipMock: (result: FinishResult) => void;
}) {
  const { t } = useTranslation();
  const [submitting, setSubmitting] = useState(false);
  const [skipping, setSkipping] = useState(false);
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

  async function onSkip() {
    setError(null);
    setSkipping(true);
    try {
      await finalizeSkipOnboard();
      onSkipMock({ kind: "mock" });
    } catch (err) {
      if (err instanceof CorlinmanApiError) {
        setError(
          t("auth.onboardFinalizeError", { detail: err.message }),
        );
      } else {
        setError(String(err));
      }
    } finally {
      setSkipping(false);
    }
  }

  const busy = submitting || skipping;

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
            disabled={busy}
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
            disabled={busy}
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
            disabled={busy}
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
        <div className="flex flex-wrap gap-2">
          {showBack ? (
            <Button
              type="button"
              variant="outline"
              onClick={onBack}
              disabled={busy}
            >
              {t("auth.onboardBack")}
            </Button>
          ) : null}
          <Button type="submit" className="flex-1" disabled={busy}>
            {submitting ? t("auth.submitting") : t("auth.onboardNext")}
          </Button>
        </div>
        <div className="rounded-md border border-tp-glass-edge bg-tp-glass-inner p-3">
          <Button
            type="button"
            variant="secondary"
            className="w-full font-semibold"
            onClick={onSkip}
            disabled={busy}
            data-testid="onboard-skip-mock"
          >
            {skipping ? t("auth.submitting") : t("auth.onboardSkipLlm")}
          </Button>
          <p className="mt-2 text-xs text-tp-ink-3">
            {t("auth.onboardSkipHint")}
          </p>
        </div>
      </form>
    </>
  );
}

// ---------------------------------------------------------------------------
// Step 3: Pick models — two-stage ModelPickerDialog per kind
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
  const [openKind, setOpenKind] = useState<ModelPickerKind | null>(null);

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

  const activeChannels =
    openKind === "llm"
      ? llmChannels
      : openKind === "embedding"
        ? embChannels
        : openKind === "tts"
          ? ttsChannels
          : [];

  const activePick =
    openKind === "llm"
      ? llmPick
      : openKind === "embedding"
        ? embPick
        : openKind === "tts"
          ? ttsPick
          : null;

  function handlePick(pick: { channel_id: number; model: string }) {
    if (openKind === "llm") {
      onLlmChange(pick);
    } else if (openKind === "embedding") {
      onEmbChange(pick);
    } else if (openKind === "tts") {
      onTtsChange({ ...pick, voice: ttsPick?.voice ?? "alloy" });
    }
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
        <ModelRow
          kind="llm"
          label={t("auth.onboardModelsLlm")}
          kindLabel={t("auth.onboardModelsKindLlm")}
          channels={llmChannels}
          pick={llmPick}
          required
          loading={loading}
          onEdit={() => setOpenKind("llm")}
          emptyHint={t("auth.onboardModelsNoLlm")}
        />
        <ModelRow
          kind="embedding"
          label={t("auth.onboardModelsEmbedding")}
          kindLabel={t("auth.onboardModelsKindEmbedding")}
          channels={embChannels}
          pick={embPick}
          loading={loading}
          onEdit={() => setOpenKind("embedding")}
          onSkip={() => onEmbChange(null)}
          emptyHint={t("auth.onboardModelsNoEmbedding")}
        />
        <ModelRow
          kind="tts"
          label={t("auth.onboardModelsTts")}
          kindLabel={t("auth.onboardModelsKindTts")}
          channels={ttsChannels}
          pick={ttsPick}
          loading={loading}
          onEdit={() => setOpenKind("tts")}
          onSkip={() => onTtsChange(null)}
          emptyHint={t("auth.onboardModelsNoTts")}
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
          <Button
            type="submit"
            className="flex-1"
            disabled={loading || !llmPick}
          >
            {t("auth.onboardNext")}
          </Button>
        </div>
      </form>
      {openKind ? (
        <ModelPickerDialog
          open={openKind !== null}
          onOpenChange={(o) => {
            if (!o) setOpenKind(null);
          }}
          providers={activeChannels}
          onPick={handlePick}
          kind={openKind}
          currentChannelId={activePick?.channel_id}
          currentModel={activePick?.model}
        />
      ) : null}
    </>
  );
}

function ModelRow({
  kind,
  label,
  kindLabel,
  channels,
  pick,
  required,
  loading,
  onEdit,
  onSkip,
  emptyHint,
}: {
  kind: ModelPickerKind;
  label: string;
  kindLabel: string;
  channels: NewapiChannel[];
  pick: ModelPickState | null;
  required?: boolean;
  loading: boolean;
  onEdit: () => void;
  onSkip?: () => void;
  emptyHint: string;
}) {
  const { t } = useTranslation();
  const hasPick = !!pick && !!pick.model;
  const noChannels = !loading && channels.length === 0;

  return (
    <div className="space-y-1.5 rounded-md border border-tp-glass-edge bg-tp-glass-inner/40 p-3">
      <div className="flex items-center justify-between gap-2">
        <Label>{label}</Label>
        {!required && onSkip && hasPick ? (
          <button
            type="button"
            onClick={onSkip}
            className="text-xs text-tp-ink-3 hover:underline"
            data-testid={`model-row-skip-${kind}`}
          >
            {t("auth.onboardModelsSkip")}
          </button>
        ) : null}
      </div>
      {noChannels ? (
        <p className="text-xs text-tp-ink-3">{emptyHint}</p>
      ) : (
        <div className="flex items-center justify-between gap-2">
          {hasPick ? (
            <p className="min-w-0 truncate font-mono text-sm">
              {t("auth.onboardModelsCurrentlyPicked", { model: pick.model })}
            </p>
          ) : (
            <p className="text-xs italic text-tp-ink-3">
              {t("auth.onboardModelsChoose", { kind: kindLabel })}
            </p>
          )}
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={onEdit}
            disabled={loading || channels.length === 0}
            data-testid={`model-row-edit-${kind}`}
          >
            {hasPick ? (
              t("auth.onboardModelsEdit")
            ) : (
              <>
                {t("auth.onboardModelsChoose", { kind: kindLabel })}
                <ChevronRight className="ml-0.5 h-3 w-3" />
              </>
            )}
          </Button>
        </div>
      )}
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
  onFinished,
}: {
  newapi: NewapiState;
  llmPick: ModelPickState | null;
  embPick: ModelPickState | null;
  ttsPick: TtsPickState | null;
  onBack: () => void;
  onFinished: (result: FinishResult) => void;
}) {
  const { t } = useTranslation();
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
      onFinished({
        kind: "real",
        provider: newapi.base_url || "newapi",
        model: llmPick.model,
      });
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

// ---------------------------------------------------------------------------
// Success card — shared between mock-skip and real-finalize paths
// ---------------------------------------------------------------------------

function SuccessCard({ result }: { result: FinishResult }) {
  const { t } = useTranslation();
  const router = useRouter();

  const subtitle =
    result.kind === "mock"
      ? t("auth.onboardCompleteMockProvider")
      : t("auth.onboardCompleteRealProvider", { provider: result.provider });

  return (
    <div
      className="space-y-5 rounded-md border border-tp-glass-edge bg-tp-glass-inner p-6"
      data-testid="onboard-success-card"
    >
      <div className="flex items-start gap-3">
        <CheckCircle2
          className="mt-0.5 h-7 w-7 text-emerald-500"
          aria-hidden
        />
        <div className="space-y-1">
          <h2 className="text-xl font-semibold tracking-tight">
            {t("auth.onboardCompleteTitle")}
          </h2>
          <p
            className="text-sm text-tp-ink-3"
            data-testid="onboard-success-subtitle"
          >
            {subtitle}
          </p>
          {result.kind === "mock" ? (
            <p className="pt-1 text-xs text-tp-ink-3">
              {t("auth.onboardSkippedBody")}
            </p>
          ) : null}
        </div>
      </div>
      <div className="flex flex-col gap-2">
        <Button
          type="button"
          className="w-full"
          onClick={() => router.replace("/account/security")}
          data-testid="onboard-cta-security"
        >
          {t("auth.onboardChangeDefaultPassword")}
        </Button>
        <Button
          type="button"
          variant="outline"
          className="w-full"
          onClick={() => router.replace("/")}
          data-testid="onboard-cta-dashboard"
        >
          {t("auth.onboardGoToDashboard")}
        </Button>
      </div>
    </div>
  );
}

// Lowercase-snake → CamelCase helper for error code → i18n key mapping.
function toCamel(code: string): string {
  return code
    .split("_")
    .map((p) => (p.length === 0 ? "" : p[0].toUpperCase() + p.slice(1)))
    .join("");
}
