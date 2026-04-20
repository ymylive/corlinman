/**
 * Admin auth — validates `AdminUsername` / `AdminPassword` (config.toml
 * `[admin]` keys) against the gateway `/admin/login` route, which sets
 * a session cookie consumed by all `/admin/*` handlers.
 *
 * TODO(M6): wire real fetch once gateway admin routes land. For M0 we only
 *           expose the shape so the layout can reference it.
 */

import { apiFetch } from "./api";

export interface LoginRequest {
  username: string;
  password: string;
}

export interface AdminSession {
  username: string;
  expiresAt: string; // ISO8601
}

export async function login(req: LoginRequest): Promise<AdminSession> {
  return apiFetch<AdminSession>("/admin/login", {
    method: "POST",
    body: req,
    mock: {
      username: req.username || "admin",
      expiresAt: new Date(Date.now() + 1000 * 60 * 60 * 8).toISOString(),
    },
  });
}

export async function logout(): Promise<void> {
  return apiFetch<void>("/admin/logout", { method: "POST", mock: undefined });
}

export async function getSession(): Promise<AdminSession | null> {
  try {
    return await apiFetch<AdminSession>("/admin/session", {
      mock: {
        username: "admin",
        expiresAt: new Date(Date.now() + 1000 * 60 * 60 * 8).toISOString(),
      },
    });
  } catch {
    return null;
  }
}
