"use client";

/**
 * <DefaultPasswordBanner /> — Wave 1.3.
 *
 * Renders a red/amber strip at the top of the admin shell whenever the
 * gateway reports `must_change_password=true` on `GET /admin/me`. Reads
 * the flag from `<MustChangePasswordContext>` (same source the guard
 * uses) so we don't double-fetch.
 *
 * Banners disappear silently when the flag is false — no skeleton, no
 * placeholder. That keeps the admin shell from "flashing" the warning
 * on every navigation for operators who have already rotated.
 */

import Link from "next/link";
import { useTranslation } from "react-i18next";
import { ShieldAlert } from "lucide-react";

import { useMustChangePassword } from "./must-change-password-context";

export function DefaultPasswordBanner() {
  const { t } = useTranslation();
  const { mustChange } = useMustChangePassword();

  if (!mustChange) return null;

  return (
    <div
      role="alert"
      data-testid="default-password-banner"
      className="flex w-full items-start gap-3 rounded-lg border border-tp-amber/40 bg-tp-amber-soft px-4 py-3 text-sm text-tp-amber shadow-sm"
    >
      <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
      <div className="flex flex-1 flex-wrap items-center justify-between gap-3">
        <p className="leading-snug">{t("auth.defaultPasswordWarning")}</p>
        <Link
          href="/account/security"
          className="inline-flex items-center rounded-md border border-tp-amber/50 bg-tp-amber/10 px-3 py-1 text-xs font-medium text-tp-amber hover:bg-tp-amber/20"
        >
          {t("auth.changeItNow")}
        </Link>
      </div>
    </div>
  );
}
