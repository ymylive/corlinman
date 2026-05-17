"use client";

/**
 * /account/security — Wave 1.3 + 1.4 (easy-setup plan).
 *
 * Two stacked cards: change username, then change password. Both
 * mutations hit `routes_admin_a/auth.py` and require the operator's
 * current password as a confirmation hash check. After a successful
 * password change we refetch `GET /admin/me` so the `must_change_password`
 * flag flips and the guard releases. The login redirect logic + the
 * default-password banner are sibling components in `components/admin/`.
 *
 * Why a dedicated page (and not a dialog like `change-password-dialog.tsx`):
 *   1. Operators arriving via the must_change_password guard need a
 *      stable landing spot — a dialog would dismiss on backdrop click.
 *   2. We want to put username + password mutations in the same place
 *      so operators don't have to hunt two menus to harden the seed.
 *   3. Borrowing the hermes-agent EnvPage paste-only + eye-icon pattern
 *      gives us per-field reveal toggles without copying its full row
 *      machinery.
 */

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { CheckCircle2, Eye, EyeOff, KeyRound, UserCog } from "lucide-react";

import {
  changePassword,
  changeUsername,
  getSession,
  type AdminSession,
} from "@/lib/auth";
import { CorlinmanApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";

const MIN_PASSWORD_LEN = 8;
const MAX_USERNAME_LEN = 64;
// Same regex the backend uses for `invalid_username` 422s. Keeping it
// here as a soft client-side hint keeps the round-trip fast; the gateway
// is the authoritative validator.
const USERNAME_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/;

export default function AccountSecurityPage() {
  const { t } = useTranslation();
  const router = useRouter();
  const [session, setSession] = useState<AdminSession | null>(null);
  const [mustChange, setMustChange] = useState<boolean | null>(null);
  // Tracks the "you just rotated, you can leave now" callout. Distinct
  // from `mustChange` because the guard reads `mustChange` directly off
  // /admin/me; the success state needs its own ack so we don't flash it
  // on every mount.
  const [justResolved, setJustResolved] = useState(false);

  // Initial load of /admin/me — feeds both the username card (as the
  // current value placeholder) and the guard-release detection. Errors
  // are swallowed; the guard wrapping this page will already have
  // redirected to /login if the session is bust.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const s = await getSession();
        if (cancelled) return;
        setSession(s);
        setMustChange(s?.must_change_password ?? false);
      } catch {
        // surface nothing; the layout guard owns auth failure UX.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  async function refreshMe() {
    const s = await getSession();
    setSession(s);
    const next = s?.must_change_password ?? false;
    setMustChange(next);
    if (!next) setJustResolved(true);
  }

  return (
    <div className="mx-auto w-full max-w-2xl space-y-6">
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">
          {t("account.security.title")}
        </h1>
        <p className="text-sm text-tp-ink-3">
          {t("account.security.subtitle")}
        </p>
      </header>

      {/* Post-rotation success notice. Only renders after a successful
          password change AND a fresh /admin/me confirms the flag flipped.
          We keep the user on the page so they can finish renaming if
          they haven't — but offer a one-click escape hatch. */}
      {justResolved && (
        <div
          role="status"
          data-testid="account-security-resolved"
          className="flex items-start gap-3 rounded-lg border border-emerald-500/30 bg-emerald-500/10 p-4 text-sm text-emerald-200"
        >
          <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
          <div className="flex-1 space-y-1">
            <p className="font-medium">
              {t("account.security.passwordChanged")}
            </p>
            <Button
              size="sm"
              variant="outline"
              className="mt-1"
              onClick={() => router.replace("/")}
            >
              {t("auth.continueDashboard")}
            </Button>
          </div>
        </div>
      )}

      <ChangeUsernameCard
        currentUsername={session?.user ?? ""}
        onSuccess={(name) => {
          toast.success(t("account.security.usernameChanged", { name }));
          // The session row is locally stale (cookie still references
          // the *new* user, but /admin/me hasn't been re-fetched yet).
          // Pull a fresh one so the "current" hint reflects reality.
          void refreshMe();
        }}
      />

      <ChangePasswordCard
        mustChange={mustChange === true}
        onSuccess={() => {
          toast.success(t("account.security.passwordChanged"));
          void refreshMe();
        }}
      />
    </div>
  );
}

// --- Username card --------------------------------------------------------

function ChangeUsernameCard({
  currentUsername,
  onSuccess,
}: {
  currentUsername: string;
  onSuccess: (newName: string) => void;
}) {
  const { t } = useTranslation();
  const [oldPassword, setOldPassword] = useState("");
  const [newUsername, setNewUsername] = useState("");
  const [showPass, setShowPass] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setError(null);
    const trimmed = newUsername.trim();
    if (!trimmed) {
      setError(t("auth.invalidUsername"));
      return;
    }
    // Cheap client-side gate so the operator doesn't burn an HTTP round
    // trip on an obvious typo. The backend is still the source of truth
    // — anything that slips through this regex still gets a 422 back.
    if (!USERNAME_RE.test(trimmed) || trimmed.length > MAX_USERNAME_LEN) {
      setError(t("auth.invalidUsername"));
      return;
    }
    setSubmitting(true);
    try {
      const res = await changeUsername({
        old_password: oldPassword,
        new_username: trimmed,
      });
      // Reset both fields so the form is ready for a follow-up edit
      // without leaking the rotated password into the next attempt.
      setOldPassword("");
      setNewUsername("");
      onSuccess(res.username);
    } catch (err) {
      setError(mapAuthError(err, t));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Card data-testid="card-change-username">
      <CardHeader>
        <div className="flex items-center gap-2">
          <UserCog className="h-4 w-4 text-tp-ink-3" aria-hidden />
          <CardTitle>{t("account.security.changeUsername")}</CardTitle>
        </div>
        <CardDescription>
          {currentUsername ? (
            <>
              <span className="text-tp-ink-3">
                {t("account.security.currentUsernameLabel")}{" "}
              </span>
              <span className="font-mono text-tp-ink-2">{currentUsername}</span>
            </>
          ) : (
            t("account.security.subtitle")
          )}
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={onSubmit} className="space-y-4" noValidate>
          <div className="space-y-2">
            <Label htmlFor="username-current-password">
              {t("account.security.currentPassword")}
            </Label>
            <PasswordField
              id="username-current-password"
              value={oldPassword}
              onChange={setOldPassword}
              show={showPass}
              onToggle={() => setShowPass((s) => !s)}
              autoComplete="current-password"
              disabled={submitting}
              testId="username-current-password"
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="new-username">
              {t("account.security.newUsername")}
            </Label>
            <Input
              id="new-username"
              data-testid="new-username"
              name="new-username"
              autoComplete="username"
              maxLength={MAX_USERNAME_LEN}
              required
              value={newUsername}
              onChange={(e) => setNewUsername(e.target.value)}
              disabled={submitting}
            />
            <p className="text-xs text-tp-ink-3">
              {t("account.security.usernameRule")}
            </p>
          </div>
          {error ? (
            <p
              role="alert"
              className="text-sm text-destructive"
              data-testid="username-error"
            >
              {error}
            </p>
          ) : null}
          <div className="flex justify-end">
            <Button
              type="submit"
              disabled={submitting}
              data-testid="username-submit"
            >
              {submitting
                ? t("common.saving")
                : t("account.security.changeUsername")}
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}

// --- Password card --------------------------------------------------------

function ChangePasswordCard({
  mustChange,
  onSuccess,
}: {
  mustChange: boolean;
  onSuccess: () => void;
}) {
  const { t } = useTranslation();
  const [oldPassword, setOldPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  // One toggle per card — flipping it reveals every password field in
  // this form, mirroring the EnvPage paste-and-verify mental model.
  const [showPass, setShowPass] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setError(null);
    // Client-side checks first so the operator gets instant feedback on
    // the "you just made a typo" cases without round-tripping the gateway.
    if (newPassword.length < MIN_PASSWORD_LEN) {
      setError(t("auth.weakPassword", { min: MIN_PASSWORD_LEN }));
      return;
    }
    if (newPassword !== confirm) {
      setError(t("account.security.passwordMismatch"));
      return;
    }
    setSubmitting(true);
    try {
      await changePassword({
        old_password: oldPassword,
        new_password: newPassword,
      });
      setOldPassword("");
      setNewPassword("");
      setConfirm("");
      onSuccess();
    } catch (err) {
      setError(mapAuthError(err, t));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Card
      data-testid="card-change-password"
      // When the operator landed here because of the seed default, the
      // password card is the action that resolves the guard. Subtly
      // signpost it so they don't miss what the page is here for.
      className={cn(
        mustChange &&
          "border-tp-amber/40 ring-1 ring-tp-amber/20 transition-colors",
      )}
    >
      <CardHeader>
        <div className="flex items-center gap-2">
          <KeyRound className="h-4 w-4 text-tp-ink-3" aria-hidden />
          <CardTitle>{t("account.security.changePassword")}</CardTitle>
        </div>
        <CardDescription>
          {mustChange
            ? t("auth.defaultPasswordWarning")
            : t("auth.changePasswordDescription")}
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={onSubmit} className="space-y-4" noValidate>
          <div className="space-y-2">
            <Label htmlFor="cpw-old">
              {t("account.security.currentPassword")}
            </Label>
            <PasswordField
              id="cpw-old"
              value={oldPassword}
              onChange={setOldPassword}
              show={showPass}
              onToggle={() => setShowPass((s) => !s)}
              autoComplete="current-password"
              disabled={submitting}
              testId="cpw-old"
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="cpw-new">
              {t("account.security.newPassword")}
            </Label>
            <PasswordField
              id="cpw-new"
              value={newPassword}
              onChange={setNewPassword}
              show={showPass}
              onToggle={() => setShowPass((s) => !s)}
              autoComplete="new-password"
              disabled={submitting}
              testId="cpw-new"
            />
            <p className="text-xs text-tp-ink-3">
              {t("account.security.passwordRule")}
            </p>
          </div>
          <div className="space-y-2">
            <Label htmlFor="cpw-confirm">
              {t("account.security.confirmNewPassword")}
            </Label>
            <PasswordField
              id="cpw-confirm"
              value={confirm}
              onChange={setConfirm}
              show={showPass}
              onToggle={() => setShowPass((s) => !s)}
              autoComplete="new-password"
              disabled={submitting}
              testId="cpw-confirm"
            />
          </div>
          {error ? (
            <p
              role="alert"
              className="text-sm text-destructive"
              data-testid="password-error"
            >
              {error}
            </p>
          ) : null}
          <div className="flex justify-end">
            <Button
              type="submit"
              disabled={submitting}
              data-testid="password-submit"
            >
              {submitting
                ? t("common.saving")
                : t("account.security.changePassword")}
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}

// --- shared: paste-only password field with eye-icon reveal --------------

function PasswordField({
  id,
  value,
  onChange,
  show,
  onToggle,
  autoComplete,
  disabled,
  testId,
}: {
  id: string;
  value: string;
  onChange: (v: string) => void;
  show: boolean;
  onToggle: () => void;
  autoComplete: string;
  disabled?: boolean;
  testId?: string;
}) {
  const { t } = useTranslation();
  return (
    <div className="relative">
      <Input
        id={id}
        data-testid={testId}
        type={show ? "text" : "password"}
        autoComplete={autoComplete}
        required
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
        className="pr-9"
      />
      <button
        type="button"
        onClick={onToggle}
        aria-label={show ? t("account.security.hide") : t("account.security.reveal")}
        aria-pressed={show}
        data-testid={`${testId ?? id}-reveal`}
        tabIndex={-1}
        className="absolute right-2 top-1/2 inline-flex h-7 w-7 -translate-y-1/2 items-center justify-center rounded-md text-tp-ink-3 transition-colors hover:bg-tp-glass-inner hover:text-tp-ink disabled:opacity-50"
        disabled={disabled}
      >
        {show ? (
          <EyeOff className="h-3.5 w-3.5" aria-hidden />
        ) : (
          <Eye className="h-3.5 w-3.5" aria-hidden />
        )}
      </button>
    </div>
  );
}

// --- error mapping --------------------------------------------------------

/**
 * Maps the backend's pydantic error codes to i18n strings.
 *
 * Body shape varies (FastAPI's 422 sometimes nests under `detail[].msg`,
 * a hand-rolled 401 ships `{error: "invalid_old_password"}`). We sniff
 * for the well-known token rather than try to parse JSON — the gateway
 * keeps the literal strings stable so substring matching is fine.
 */
function mapAuthError(
  err: unknown,
  t: ReturnType<typeof useTranslation>["t"],
): string {
  if (err instanceof CorlinmanApiError) {
    const msg = err.message ?? "";
    if (err.status === 401) {
      if (msg.includes("session_user_mismatch")) {
        return t("auth.invalidOldPassword");
      }
      return t("auth.invalidOldPassword");
    }
    if (err.status === 422) {
      if (msg.includes("invalid_username")) {
        return t("auth.invalidUsername");
      }
      if (msg.includes("weak_password")) {
        return t("auth.weakPassword", { min: MIN_PASSWORD_LEN });
      }
      // Generic 422 — surface the raw message so the operator can self-diagnose.
      return msg || t("common.error");
    }
    return msg || t("common.error");
  }
  return String(err);
}
