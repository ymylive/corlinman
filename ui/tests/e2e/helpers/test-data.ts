/**
 * Test-data seed / teardown helpers for Wave 5.1 Playwright specs.
 *
 * These talk to the gateway HTTP API directly via Playwright's
 * `request` fixture so we exercise the real boundary (no direct sqlite
 * pokes). Each helper is idempotent — calling `deleteProfileIfExists`
 * on a profile that's already gone is a no-op, and `resetAdminPassword`
 * will skip the rotation if the current password is already the target.
 *
 * The base URL is read from `PLAYWRIGHT_GATEWAY_URL` (default
 * `http://localhost:6005`) so CI can point at a containerised gateway
 * without code changes.
 */

import { expect, type APIRequestContext, type Page } from "@playwright/test";

import {
  DEFAULT_ADMIN_PASSWORD,
  DEFAULT_ADMIN_USER,
} from "./auth";

export const GATEWAY_URL =
  process.env.PLAYWRIGHT_GATEWAY_URL ?? "http://localhost:6005";

/** Profile slug protected by the gateway — never delete it. */
export const PROTECTED_PROFILE_SLUG = "default";

export interface ProfileWire {
  slug: string;
  display_name: string;
  parent_slug: string | null;
  description: string | null;
  created_at: string;
}

/**
 * POST /admin/login with the supplied credentials. Returns the
 * `Set-Cookie` token so callers can use it on subsequent requests
 * without re-authenticating.
 *
 * Throws if the credentials are wrong (so callers can `try { } catch`
 * to detect "already rotated" mid-test).
 */
export async function apiLogin(
  request: APIRequestContext,
  username: string = DEFAULT_ADMIN_USER,
  password: string = DEFAULT_ADMIN_PASSWORD,
): Promise<void> {
  const res = await request.post(`${GATEWAY_URL}/admin/login`, {
    data: { username, password },
  });
  if (!res.ok()) {
    throw new Error(
      `apiLogin failed (${res.status()}): ${await res.text().catch(() => "")}`,
    );
  }
}

export async function apiLogout(request: APIRequestContext): Promise<void> {
  await request.post(`${GATEWAY_URL}/admin/logout`).catch(() => {
    /* idempotent — gateway returns 200 even on stale cookies. */
  });
}

export async function apiListProfiles(
  request: APIRequestContext,
): Promise<ProfileWire[]> {
  const res = await request.get(`${GATEWAY_URL}/admin/profiles`);
  if (!res.ok()) {
    throw new Error(`listProfiles failed (${res.status()})`);
  }
  return (await res.json()) as ProfileWire[];
}

export async function apiCreateProfile(
  request: APIRequestContext,
  body: {
    slug: string;
    display_name?: string;
    clone_from?: string;
    description?: string;
  },
): Promise<ProfileWire> {
  const res = await request.post(`${GATEWAY_URL}/admin/profiles`, {
    data: body,
  });
  if (!res.ok()) {
    throw new Error(
      `createProfile(${body.slug}) failed (${res.status()}): ` +
        (await res.text().catch(() => "")),
    );
  }
  return (await res.json()) as ProfileWire;
}

export async function apiDeleteProfile(
  request: APIRequestContext,
  slug: string,
): Promise<void> {
  if (slug === PROTECTED_PROFILE_SLUG) return;
  const res = await request.delete(
    `${GATEWAY_URL}/admin/profiles/${encodeURIComponent(slug)}`,
  );
  // 404 = already gone (idempotent). Anything else flag.
  if (!res.ok() && res.status() !== 404) {
    throw new Error(`deleteProfile(${slug}) failed (${res.status()})`);
  }
}

/**
 * Delete a profile only if it currently exists. Useful for teardown
 * where the spec may have failed before the create step ran.
 */
export async function apiDeleteProfileIfExists(
  request: APIRequestContext,
  slug: string,
): Promise<void> {
  if (slug === PROTECTED_PROFILE_SLUG) return;
  try {
    const profiles = await apiListProfiles(request);
    if (profiles.some((p) => p.slug === slug)) {
      await apiDeleteProfile(request, slug);
    }
  } catch {
    /* unauthenticated or gateway down — leave it to a later cleanup. */
  }
}

/**
 * Delete every profile created by the test suite (slug starts with one
 * of the supplied prefixes). Skips the protected `default`.
 */
export async function apiPurgeTestProfiles(
  request: APIRequestContext,
  prefixes: string[],
): Promise<void> {
  try {
    const profiles = await apiListProfiles(request);
    for (const p of profiles) {
      if (p.slug === PROTECTED_PROFILE_SLUG) continue;
      if (prefixes.some((pre) => p.slug.startsWith(pre))) {
        await apiDeleteProfile(request, p.slug);
      }
    }
  } catch {
    /* swallow — best-effort cleanup. */
  }
}

/**
 * Force the admin password back to `DEFAULT_ADMIN_PASSWORD`. Tries
 * `currentPassword` first; if that fails (already at default) we
 * silently succeed. Used in spec teardown so the next spec / run
 * starts from a known state.
 *
 * Note: this rotates the password ONLY if the operator is signed in
 * with `currentPassword`. The caller is responsible for re-logging in
 * with the right credentials if it doesn't know the current state.
 */
export async function resetAdminPassword(
  request: APIRequestContext,
  currentPassword: string,
  username: string = DEFAULT_ADMIN_USER,
): Promise<void> {
  if (currentPassword === DEFAULT_ADMIN_PASSWORD) return;
  try {
    await apiLogin(request, username, currentPassword);
    const res = await request.post(`${GATEWAY_URL}/admin/password`, {
      data: {
        old_password: currentPassword,
        new_password: DEFAULT_ADMIN_PASSWORD,
      },
    });
    if (!res.ok()) {
      // Already at default, or the spec rotated more than once and we
      // missed an intermediate state. Either way, leave it alone — the
      // next spec will re-discover the state via login attempts.
    }
  } catch {
    /* swallow */
  } finally {
    await apiLogout(request);
  }
}

/**
 * Force the admin username back to `admin`. Same caveats as
 * `resetAdminPassword`.
 */
export async function resetAdminUsername(
  request: APIRequestContext,
  currentUsername: string,
  currentPassword: string,
): Promise<void> {
  if (currentUsername === DEFAULT_ADMIN_USER) return;
  try {
    await apiLogin(request, currentUsername, currentPassword);
    const res = await request.post(`${GATEWAY_URL}/admin/username`, {
      data: {
        old_password: currentPassword,
        new_username: DEFAULT_ADMIN_USER,
      },
    });
    if (!res.ok()) {
      /* swallow */
    }
  } catch {
    /* swallow */
  } finally {
    await apiLogout(request);
  }
}

/**
 * Wait for the gateway to be reachable. Returns true if a healthy 200
 * came back within `timeoutMs`, false otherwise. Specs can use this to
 * `test.skip(!up, ...)` when the harness didn't bring the backend up.
 */
export async function waitForGateway(
  page: Page,
  timeoutMs = 5_000,
): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const res = await page.request.get(`${GATEWAY_URL}/health`, {
        timeout: 1_500,
      });
      if (res.ok()) return true;
    } catch {
      /* retry */
    }
    await new Promise((r) => setTimeout(r, 250));
  }
  return false;
}

/**
 * Convenience: assert the admin/root seed is currently in place. The
 * specs use this in their top-level beforeAll so they can fail fast
 * with a useful error if the prior spec didn't clean up.
 */
export async function expectDefaultAdminSeed(
  request: APIRequestContext,
): Promise<void> {
  try {
    await apiLogin(request, DEFAULT_ADMIN_USER, DEFAULT_ADMIN_PASSWORD);
    expect(true).toBe(true);
  } catch (err) {
    throw new Error(
      `Default admin/root seed not present. Prior spec teardown likely ` +
        `failed — delete config.toml and restart the gateway. Cause: ` +
        `${String(err)}`,
    );
  } finally {
    await apiLogout(request);
  }
}
