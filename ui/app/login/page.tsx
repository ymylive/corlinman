"use client";

/**
 * Admin login page. Sits outside the `(admin)` route group so its layout
 * stays minimal (no sidebar, no top nav) and it never triggers the guard
 * in `(admin)/layout.tsx` — which would create an infinite redirect loop.
 *
 * Flow:
 *   1. User types username + password → submits.
 *   2. We POST `/admin/login`; the gateway validates argon2 + sets the
 *      `corlinman_session` HttpOnly cookie on the response.
 *   3. On success, navigate to `?redirect=<path>` if present, else `/`.
 *   4. On failure, render the error string inline (no toast lib yet).
 *
 * `useSearchParams` is wrapped in `<Suspense>` so Next.js can prerender
 * the shell during `next build` without bailing out on CSR-only hooks.
 */

import { Suspense, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import { login } from "@/lib/auth";
import { CorlinmanApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export default function LoginPage() {
  return (
    <div className="flex min-h-dvh items-center justify-center bg-muted/30 p-6">
      <Suspense fallback={<LoginCard disabled />}>
        <LoginForm />
      </Suspense>
    </div>
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
              ? "管理员凭据未配置（config.toml [admin]）"
              : err.message,
        );
      } else {
        setError(String(err));
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Card className="w-full max-w-sm">
      <CardHeader>
        <CardTitle>corlinman admin</CardTitle>
        <CardDescription>登录以进入管理后台</CardDescription>
      </CardHeader>
      <form onSubmit={onSubmit}>
        <CardContent className="space-y-4">
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
        </CardContent>
        <CardFooter>
          <Button type="submit" className="w-full" disabled={submitting}>
            {submitting ? "登录中..." : "登录"}
          </Button>
        </CardFooter>
      </form>
    </Card>
  );
}

/** Skeleton rendered while the Suspense boundary is pending. */
function LoginCard({ disabled }: { disabled?: boolean }) {
  return (
    <Card className="w-full max-w-sm">
      <CardHeader>
        <CardTitle>corlinman admin</CardTitle>
        <CardDescription>登录以进入管理后台</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="space-y-2">
          <Label htmlFor="username">用户名</Label>
          <Input id="username" disabled={disabled} />
        </div>
        <div className="space-y-2">
          <Label htmlFor="password">密码</Label>
          <Input id="password" type="password" disabled={disabled} />
        </div>
      </CardContent>
      <CardFooter>
        <Button type="button" className="w-full" disabled>
          登录
        </Button>
      </CardFooter>
    </Card>
  );
}
