/**
 * Admin auth client. Wraps `POST /admin/login`, `POST /admin/logout`,
 * `GET /admin/me` on the gateway.
 *
 * The session cookie (`corlinman_session`) is set HttpOnly by the gateway,
 * so JS can't read it — we rely on `credentials: "include"` (already baked
 * into `apiFetch`) to have the browser round-trip it automatically.
 */

import { apiFetch, CorlinmanApiError } from "./api";

export interface LoginRequest {
  username: string;
  password: string;
}

/** Response body of `POST /admin/login`. `token` mirrors the cookie. */
export interface LoginResponse {
  token: string;
  expires_in: number;
}

/** Shape of `GET /admin/me`. Fields are ISO-8601 UTC strings. */
export interface AdminSession {
  user: string;
  created_at: string;
  expires_at: string;
}

/** POST `/admin/login`. Throws `CorlinmanApiError` on 401 / 503. */
export function login(req: LoginRequest): Promise<LoginResponse> {
  return apiFetch<LoginResponse>("/admin/login", {
    method: "POST",
    body: req,
  });
}

/** POST `/admin/logout`. Idempotent: succeeds even without a cookie. */
export function logout(): Promise<void> {
  return apiFetch<void>("/admin/logout", { method: "POST" });
}

/**
 * GET `/admin/me`. Returns `null` on 401 rather than throwing, so callers
 * can branch on unauthenticated state without try/catch noise.
 */
export async function getSession(): Promise<AdminSession | null> {
  try {
    return await apiFetch<AdminSession>("/admin/me");
  } catch (err) {
    if (err instanceof CorlinmanApiError && err.status === 401) {
      return null;
    }
    throw err;
  }
}

export interface OnboardRequest {
  username: string;
  password: string;
}

/**
 * `POST /admin/onboard` — first-run admin bootstrap. The gateway only
 * accepts this while the `[admin]` block is empty; afterwards it returns
 * 409 `already_onboarded`. UI flow: probe `/admin/login` and redirect
 * here when it returns 503 `admin_not_configured`.
 */
export function onboard(req: OnboardRequest): Promise<void> {
  return apiFetch<void>("/admin/onboard", {
    method: "POST",
    body: req,
  });
}

export interface ChangePasswordRequest {
  old_password: string;
  new_password: string;
}

/**
 * `POST /admin/password` — rotate the logged-in admin's password.
 * Requires a valid session cookie + correct `old_password`. The gateway
 * argon2-verifies the old hash and rewrites `config.toml` atomically on
 * success. 401 on bad old password, 422 on a new password shorter than
 * the gateway-side minimum.
 */
export function changePassword(req: ChangePasswordRequest): Promise<void> {
  return apiFetch<void>("/admin/password", {
    method: "POST",
    body: req,
  });
}
