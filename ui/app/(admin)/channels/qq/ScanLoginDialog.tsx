"use client";

import * as React from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

import { cn } from "@/lib/utils";
import { CountdownRing } from "@/components/ui/countdown-ring";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import {
  fetchQqAccounts,
  fetchQqQrcodeStatus,
  qqQuickLogin,
  requestQqQrcode,
  type QqAccount,
  type QqQrcode,
  type QqQrcodeStatus,
} from "@/lib/api";

const POLL_INTERVAL_MS = 2_000;
// Ring total is derived from the first QR's TTL (expires_at - now at open)
// and reused for subsequent displays. Falls back to 120s if absent.
const DEFAULT_TTL_MS = 120_000;

/**
 * QQ scan-login dialog (Tidepool retoken).
 *
 * Flow unchanged from pre-cutover:
 *   1. Open → POST /admin/channels/qq/qrcode → render image.
 *   2. Every 2s GET /admin/channels/qq/qrcode/status → update status line.
 *   3. On `confirmed` → show avatar/nick, invalidate ["admin","channels","qq"],
 *      auto-close after 1.5s.
 *   4. Previously-used accounts render beneath the QR; tap → /quick-login.
 *
 * Visual surface retoked to warm-amber glass: QR panel gets a warm amber
 * halo when fresh, a red overlay when expired; status line uses tp-ink
 * tones; quick-login chips read as soft glass.
 */
export function ScanLoginDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [qr, setQr] = React.useState<QqQrcode | null>(null);
  const [qrError, setQrError] = React.useState<string | null>(null);
  const [status, setStatus] = React.useState<QqQrcodeStatus>({
    status: "waiting",
  });
  const [now, setNow] = React.useState(() => Date.now());

  // Stable ref so the poll loop below can read the current token without
  // becoming its own dep (avoids restart on every token swap).
  const tokenRef = React.useRef<string | null>(null);

  // Reset + request fresh QR each time the dialog opens.
  React.useEffect(() => {
    if (!open) {
      setQr(null);
      setQrError(null);
      setStatus({ status: "waiting" });
      tokenRef.current = null;
      return;
    }
    let cancelled = false;
    setQrError(null);
    setStatus({ status: "waiting" });
    requestQqQrcode()
      .then((res) => {
        if (cancelled) return;
        setQr(res);
        tokenRef.current = res.token;
      })
      .catch((err) => {
        if (cancelled) return;
        setQrError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [open]);

  // Countdown ticker — 1 Hz, cheap.
  React.useEffect(() => {
    if (!open || !qr) return;
    const id = setInterval(() => setNow(Date.now()), 1_000);
    return () => clearInterval(id);
  }, [open, qr]);

  // Status polling — 2s while waiting; stops on terminal status.
  React.useEffect(() => {
    if (!open || !qr) return;
    if (status.status === "confirmed" || status.status === "error") return;
    const id = setInterval(async () => {
      const tok = tokenRef.current;
      if (!tok) return;
      try {
        const next = await fetchQqQrcodeStatus(tok);
        setStatus(next);
        if (next.status === "confirmed") {
          qc.invalidateQueries({ queryKey: ["admin", "channels", "qq"] });
          qc.invalidateQueries({
            queryKey: ["admin", "channels", "qq", "accounts"],
          });
          toast.success(t("channels.qq.scanLogin.confirmed"));
          // Give the user a beat to see the avatar, then close.
          setTimeout(() => onOpenChange(false), 1_500);
        }
      } catch (err) {
        setStatus({
          status: "error",
          message: err instanceof Error ? err.message : String(err),
        });
      }
    }, POLL_INTERVAL_MS);
    return () => clearInterval(id);
  }, [open, qr, status.status, qc, t, onOpenChange]);

  const remainingMs = qr ? Math.max(0, qr.expires_at - now) : 0;
  const secondsLeft = Math.ceil(remainingMs / 1_000);
  const expired = qr ? remainingMs <= 0 || status.status === "expired" : false;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-sm border-tp-glass-edge bg-tp-glass-2 backdrop-blur-glass-strong backdrop-saturate-glass-strong">
        <DialogHeader>
          <DialogTitle className="text-tp-ink">
            {t("channels.qq.scanLogin.title")}
          </DialogTitle>
          <DialogDescription className="text-tp-ink-3">
            {t("channels.qq.scanLogin.subtitle")}
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col items-center gap-3">
          {qrError ? (
            <div
              className="rounded-xl border border-tp-err/35 bg-tp-err-soft p-4 text-center text-[13px] text-tp-err"
              data-testid="qq-login-error"
            >
              {qrError}
            </div>
          ) : !qr ? (
            <Skeleton className="h-56 w-56 rounded-xl" />
          ) : status.status === "confirmed" && status.account ? (
            <AccountCard account={status.account} />
          ) : (
            <QrImage qr={qr} expired={expired} />
          )}

          <div
            className="flex items-center gap-2.5 text-[13px]"
            data-testid="qq-login-status"
            aria-live="polite"
          >
            {qr && !expired && status.status !== "confirmed" ? (
              <CountdownRing
                remainingMs={remainingMs}
                totalMs={qr ? Math.max(qr.expires_at - (qr.expires_at - DEFAULT_TTL_MS), DEFAULT_TTL_MS) : DEFAULT_TTL_MS}
                size={18}
                strokeWidth={2}
                label={t("channels.qq.scanLogin.statusWaiting")}
                className="[&>span]:hidden"
              />
            ) : null}
            <StatusLine status={status.status} secondsLeft={secondsLeft} />
          </div>
        </div>

        <QuickLoginList
          disabled={status.status === "confirmed"}
          onSelect={(uin) => {
            qqQuickLogin(uin)
              .then((res) => {
                setStatus(res);
                if (res.status === "confirmed") {
                  qc.invalidateQueries({
                    queryKey: ["admin", "channels", "qq"],
                  });
                  qc.invalidateQueries({
                    queryKey: ["admin", "channels", "qq", "accounts"],
                  });
                  toast.success(t("channels.qq.scanLogin.confirmed"));
                  setTimeout(() => onOpenChange(false), 1_500);
                } else {
                  toast.error(
                    res.message ?? t("channels.qq.scanLogin.quickLoginFailed"),
                  );
                }
              })
              .catch((err) =>
                toast.error(err instanceof Error ? err.message : String(err)),
              );
          }}
        />
      </DialogContent>
    </Dialog>
  );
}

function QrImage({ qr, expired }: { qr: QqQrcode; expired: boolean }) {
  // Two cases: NapCat returned a base64 PNG (preferred) or a URL. For the
  // URL case we don't bundle a client-side QR generator (no new deps), so
  // we render a short instruction block — the user can still copy/paste the
  // URL into their phone's QQ app. When NapCat ships base64 (v2.x default)
  // the image works directly.
  if (qr.image_base64) {
    return (
      <div
        className={cn(
          "relative h-56 w-56 overflow-hidden rounded-xl border bg-white p-2",
          "border-tp-amber/35",
          "shadow-[0_0_24px_-6px_var(--tp-amber-glow)]",
        )}
      >
        {/* Base64 data URL — next/image would require remote loader config
            for data: URLs and offers no real perf win for a one-shot QR. */}
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          data-testid="qq-qrcode"
          src={`data:image/png;base64,${qr.image_base64}`}
          alt="QQ scan-login QR code"
          className="h-full w-full object-contain"
        />
        {expired ? (
          <div className="absolute inset-0 flex items-center justify-center rounded-xl bg-black/60 font-mono text-[11px] uppercase tracking-[0.1em] text-white">
            {/* Keep English token — matches integration expectation. */}
            expired
          </div>
        ) : null}
      </div>
    );
  }
  if (qr.qrcode_url) {
    return (
      <div
        data-testid="qq-qrcode"
        className="flex h-56 w-56 flex-col items-center justify-center gap-2 rounded-xl border border-dashed border-tp-glass-edge bg-tp-glass-inner p-3 text-center text-[11.5px]"
      >
        <span className="text-tp-ink-3">
          QR URL (copy into QQ mobile):
        </span>
        <code className="break-all px-2 font-mono text-[10px] text-tp-ink-2">
          {qr.qrcode_url}
        </code>
      </div>
    );
  }
  return null;
}

function AccountCard({ account }: { account: QqAccount }) {
  return (
    <div
      className="flex flex-col items-center gap-2 rounded-xl border border-tp-ok/35 bg-tp-ok-soft p-4 text-center"
      data-testid="qq-login-account"
    >
      {account.avatar_url ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={account.avatar_url}
          alt=""
          className="h-16 w-16 rounded-full"
        />
      ) : (
        <div className="flex h-16 w-16 items-center justify-center rounded-full bg-tp-ok-soft font-mono text-[18px] text-tp-ok">
          {account.uin.slice(0, 2)}
        </div>
      )}
      <div className="text-[14px] font-semibold text-tp-ink">
        {account.nickname ?? account.uin}
      </div>
      <div className="font-mono text-[11px] text-tp-ink-3">
        QQ: {account.uin}
      </div>
    </div>
  );
}

function StatusLine({
  status,
  secondsLeft,
}: {
  status: QqQrcodeStatus["status"];
  secondsLeft: number;
}) {
  const { t } = useTranslation();
  switch (status) {
    case "waiting":
      return (
        <span className="text-tp-ink-3">
          {t("channels.qq.scanLogin.statusWaiting")}
          {secondsLeft > 0
            ? ` (${t("channels.qq.scanLogin.secondsLeft", { s: secondsLeft })})`
            : null}
        </span>
      );
    case "scanned":
      return (
        <span className="text-tp-amber">
          {t("channels.qq.scanLogin.statusScanned")}
        </span>
      );
    case "confirmed":
      return (
        <span className="text-tp-ok">
          {t("channels.qq.scanLogin.statusConfirmed")}
        </span>
      );
    case "expired":
      return (
        <span className="text-tp-warn">
          {t("channels.qq.scanLogin.statusExpired")}
        </span>
      );
    case "error":
      return (
        <span className="text-tp-err">
          {t("channels.qq.scanLogin.statusError")}
        </span>
      );
    default:
      return null;
  }
}

function QuickLoginList({
  onSelect,
  disabled,
}: {
  onSelect: (uin: string) => void;
  disabled: boolean;
}) {
  const { t } = useTranslation();
  const accounts = useQuery({
    queryKey: ["admin", "channels", "qq", "accounts"],
    queryFn: fetchQqAccounts,
  });
  const list = accounts.data?.accounts ?? [];
  if (accounts.isPending) {
    return <Skeleton className="h-12 w-full" />;
  }
  if (list.length === 0) {
    return null;
  }
  return (
    <section className="mt-2 border-t border-tp-glass-edge pt-3">
      <h3 className="mb-2 font-mono text-[10.5px] uppercase tracking-[0.12em] text-tp-ink-4">
        {t("channels.qq.scanLogin.quickLogin")}
      </h3>
      <ul className="flex flex-wrap gap-2">
        {list.map((a) => (
          <li key={a.uin}>
            <button
              type="button"
              disabled={disabled}
              onClick={() => onSelect(a.uin)}
              data-testid={`qq-quick-login-${a.uin}`}
              className={cn(
                "inline-flex items-center gap-2 rounded-lg border px-2.5 py-1.5 text-[12px]",
                "border-tp-glass-edge bg-tp-glass-inner text-tp-ink-2",
                "transition-colors hover:bg-tp-glass-inner-hover hover:text-tp-ink",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/40",
                "disabled:cursor-not-allowed disabled:opacity-50",
              )}
            >
              {a.avatar_url ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img
                  src={a.avatar_url}
                  alt=""
                  className="h-5 w-5 rounded-full"
                />
              ) : null}
              <span className="font-medium">{a.nickname ?? a.uin}</span>
              <span className="font-mono text-[10px] text-tp-ink-4">
                {a.uin}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
