"use client";

/**
 * <MustChangePasswordGuard /> — Wave 1.4.
 *
 * Hard-redirects the operator to `/account/security` whenever the
 * gateway reports `must_change_password=true` AND they are not already
 * on that page. Sits inside <MustChangePasswordProvider /> in the admin
 * layout, so it shares the flag with <DefaultPasswordBanner />.
 *
 * The guard is intentionally a thin effect:
 *   - It never blocks render. Pages still mount; the guard just bounces
 *     once the effect fires. That keeps the back/forward UX consistent
 *     (the in-flight render disappears the moment we replace history).
 *   - It uses `router.replace` not `push` so the bypassed route doesn't
 *     pollute the history stack — back button after rotation goes to
 *     `/login`, not `/admin/whatever`.
 *
 * Soft exception: the security page itself is whitelisted so we don't
 * infinite-loop while the operator is doing the work.
 */

import * as React from "react";
import { usePathname, useRouter } from "next/navigation";

import { useMustChangePassword } from "./must-change-password-context";

/** Path the guard sends operators to. Exported for the login redirect. */
export const FORCE_PATH = "/account/security";

export function MustChangePasswordGuard({
  children,
}: {
  children: React.ReactNode;
}) {
  const router = useRouter();
  const pathname = usePathname();
  const { mustChange } = useMustChangePassword();

  React.useEffect(() => {
    if (!mustChange) return;
    if (pathname === FORCE_PATH) return;
    router.replace(FORCE_PATH);
  }, [mustChange, pathname, router]);

  return <>{children}</>;
}
