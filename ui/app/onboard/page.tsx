"use client";

/**
 * First-run onboarding page. Mirrors the login layout: brand on the
 * left, two-field-plus-confirm form on the right. The gateway only
 * accepts `POST /admin/onboard` while the `[admin]` block in
 * `config.toml` is empty; afterwards the route returns 409
 * `already_onboarded` and the form will surface a redirect-to-login
 * banner.
 *
 * Flow:
 *   1. Operator picks a username + password (twice for confirmation).
 *   2. Client-side: assert the two password fields match and the
 *      password meets the 8-char floor. Server-side enforces the same
 *      floor — the client check is just to fail fast.
 *   3. `POST /admin/onboard`. On 200 navigate to `/login` so the
 *      operator can sign in with the new credentials.
 *   4. On 409 → already configured, redirect to `/login`.
 *   5. On other errors render inline.
 */

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useTranslation } from "react-i18next";

import { onboard } from "@/lib/auth";
import { CorlinmanApiError } from "@/lib/api";
import { BrandMark } from "@/components/layout/brand-mark";
import { LanguageToggle } from "@/components/layout/language-toggle";
import { ThemeToggle } from "@/components/layout/theme-toggle";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

const MIN_PASSWORD_LEN = 8;

export default function OnboardPage() {
  return (
    <div className="relative grid min-h-dvh grid-cols-1 bg-background md:grid-cols-[40%_60%]">
      <div className="absolute right-4 top-4 z-10 flex items-center gap-2">
        <LanguageToggle />
        <ThemeToggle />
      </div>
      <HeroColumn />
      <div className="flex items-center justify-center p-8">
        <OnboardForm />
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
        <span className="font-mono">v0.1.1</span>
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

function OnboardForm() {
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
      // Onboarding complete → go to login. We deliberately don't auto-
      // login here so the operator sees the new creds work end-to-end.
      router.replace("/login");
    } catch (err) {
      if (err instanceof CorlinmanApiError) {
        if (err.status === 409) {
          // Already onboarded — the safest place to send them is the
          // login page; show a brief banner first so they know why.
          setError(t("auth.onboardAlreadyConfigured"));
          // Slight delay so the user can read the message before bounce.
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
    <div className="w-full max-w-sm space-y-6">
      <div className="space-y-1.5 md:hidden">
        <BrandMark />
      </div>
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
      <p className="text-center text-xs text-tp-ink-3">
        {t("auth.onboardHint")}
      </p>
    </div>
  );
}
