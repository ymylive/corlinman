"use client";

/**
 * Admin login page. Two-column layout: brand + dot-grid art on the left,
 * form on the right. Sits outside the `(admin)` group so it doesn't
 * trigger the auth guard.
 *
 * Flow:
 *   1. User types username + password → submits.
 *   2. We POST `/admin/login`; the gateway validates argon2 + sets the
 *      `corlinman_session` HttpOnly cookie on the response.
 *   3. On success, navigate to `?redirect=<path>` if present, else `/`.
 *   4. On failure, render the error inline with a shake animation.
 */

import { Suspense, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import { login } from "@/lib/auth";
import { CorlinmanApiError } from "@/lib/api";
import { BrandMark } from "@/components/layout/brand-mark";
import { ThemeToggle } from "@/components/layout/theme-toggle";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export default function LoginPage() {
  return (
    <div className="relative grid min-h-dvh grid-cols-1 md:grid-cols-[40%_60%]">
      {/* theme toggle in top-right regardless of column */}
      <div className="absolute right-4 top-4 z-10">
        <ThemeToggle />
      </div>
      <HeroColumn />
      <div className="flex items-center justify-center p-8">
        <Suspense fallback={<LoginFormShell disabled />}>
          <LoginForm />
        </Suspense>
      </div>
    </div>
  );
}

function HeroColumn() {
  return (
    <aside className="relative hidden overflow-hidden border-r border-border bg-surface/60 md:flex md:flex-col md:justify-between md:p-10">
      <div className="flex items-center gap-2">
        <BrandMark />
      </div>
      <div className="relative z-10 space-y-2">
        <h2 className="text-lg font-semibold tracking-tight">
          Run agents, route tools, keep the edge boring.
        </h2>
        <p className="max-w-xs text-sm text-muted-foreground">
          Rust gateway, Python AI layer, static admin UI. All in one control
          plane.
        </p>
      </div>
      <div className="flex items-center gap-2 text-xs text-muted-foreground">
        <span className="font-mono">v0.1.1</span>
        <span>·</span>
        <span>M6 admin</span>
      </div>
      {/* decorative dot grid */}
      <div
        className="pointer-events-none absolute inset-0 dot-grid opacity-60"
        aria-hidden
      />
      {/* subtle radial glow */}
      <div
        className="pointer-events-none absolute inset-0 bg-[radial-gradient(600px_300px_at_20%_20%,hsl(var(--primary)/0.15),transparent_60%)]"
        aria-hidden
      />
    </aside>
  );
}

function LoginForm() {
  const router = useRouter();
  const params = useSearchParams();
  const redirect = params.get("redirect") ?? "/";

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [shakeKey, setShakeKey] = useState(0);

  async function onSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await login({ username, password });
      router.replace(redirect);
    } catch (err) {
      if (err instanceof CorlinmanApiError) {
        setError(
          err.status === 401
            ? "用户名或密码错误"
            : err.status === 503
              ? "管理员凭据未配置 (config.toml [admin])"
              : err.message,
        );
      } else {
        setError(String(err));
      }
      setShakeKey((k) => k + 1);
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
        <h1 className="text-xl font-semibold tracking-tight">Sign in</h1>
        <p className="text-sm text-muted-foreground">
          登录以进入 corlinman 管理后台。
        </p>
      </div>
      <form
        onSubmit={onSubmit}
        className="space-y-4"
        key={shakeKey}
        // Trigger the shake via key-remount + the global keyframe (globals.css).
        style={error ? { animation: "login-shake 220ms ease-out" } : undefined}
      >
        <div className="space-y-2">
          <Label htmlFor="username">用户名</Label>
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
          <Label htmlFor="password">密码</Label>
          <Input
            id="password"
            name="password"
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            disabled={submitting}
          />
        </div>
        {error ? (
          <p
            role="alert"
            className="text-sm text-destructive"
            data-testid="login-error"
          >
            {error}
          </p>
        ) : null}
        <Button type="submit" className="w-full" disabled={submitting}>
          {submitting ? "登录中..." : "登录"}
        </Button>
      </form>
      <p className="text-center text-xs text-muted-foreground">
        Session backed by argon2 · HttpOnly cookie.
      </p>
    </div>
  );
}

function LoginFormShell({ disabled }: { disabled?: boolean }) {
  return (
    <div className="w-full max-w-sm space-y-6">
      <div className="space-y-1">
        <h1 className="text-xl font-semibold tracking-tight">Sign in</h1>
        <p className="text-sm text-muted-foreground">
          登录以进入 corlinman 管理后台。
        </p>
      </div>
      <div className="space-y-4">
        <div className="space-y-2">
          <Label htmlFor="username">用户名</Label>
          <Input id="username" disabled={disabled} />
        </div>
        <div className="space-y-2">
          <Label htmlFor="password">密码</Label>
          <Input id="password" type="password" disabled={disabled} />
        </div>
        <Button type="button" className="w-full" disabled>
          登录
        </Button>
      </div>
    </div>
  );
}
