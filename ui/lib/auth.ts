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
export async function login(req: LoginRequest): Promise<LoginResponse> {
  return apiFetch<LoginResponse>("/admin/login", {
    method: "POST",
    body: req,
    mock: {
      token: "mock-session-token",
      expires_in: 86400,
    },
  });
}

/** POST `/admin/logout`. Idempotent: succeeds even without a cookie. */
export async function logout(): Promise<void> {
  return apiFetch<void>("/admin/logout", {
    method: "POST",
    mock: undefined,
  });
}

/**
 * GET `/admin/me`. Returns `null` on 401 rather than throwing, so callers
 * can branch on unauthenticated state without try/catch noise.
 */
export async function getSession(): Promise<AdminSession | null> {
  try {
    return await apiFetch<AdminSession>("/admin/me", {
      mock: {
        user: "admin",
        created_at: new Date().toISOString(),
        expires_at: new Date(Date.now() + 1000 * 60 * 60 * 24).toISOString(),
      },
    });
  } catch (err) {
    if (err instanceof CorlinmanApiError && err.status === 401) {
      return null;
    }
    throw err;
  }
}
