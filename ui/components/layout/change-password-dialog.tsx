"use client";

/**
 * Change-password dialog. Mounted by the sidebar so it can be opened
 * from the user chip without polluting the global layout tree.
 *
 * Three-field form (current / new / confirm) → `POST /admin/password`.
 * Client-side checks (match + min length) run before the network call
 * so the operator sees instant feedback on obvious mistakes; the
 * gateway re-enforces the same rules so we don't trust the client.
 *
 * Error mapping:
 *   - 401 → "current password is incorrect" (the dialog stays open so
 *     the operator can retry without losing their typed-in new pass).
 *   - 422 → "weak password" (re-uses the i18n string from onboarding).
 *   - Other → fall back to the gateway's raw message.
 */

import { useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

import { changePassword } from "@/lib/auth";
import { CorlinmanApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

const MIN_PASSWORD_LEN = 8;

interface ChangePasswordDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function ChangePasswordDialog({
  open,
  onOpenChange,
}: ChangePasswordDialogProps) {
  const { t } = useTranslation();
  const [oldPassword, setOldPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function reset() {
    setOldPassword("");
    setNewPassword("");
    setConfirm("");
    setError(null);
    setSubmitting(false);
  }

  function handleOpenChange(next: boolean) {
    if (!next) reset();
    onOpenChange(next);
  }

  async function onSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setError(null);
    if (newPassword.length < MIN_PASSWORD_LEN) {
      setError(t("auth.changePasswordWeak", { min: MIN_PASSWORD_LEN }));
      return;
    }
    if (newPassword !== confirm) {
      setError(t("auth.changePasswordMismatch"));
      return;
    }
    setSubmitting(true);
    try {
      await changePassword({
        old_password: oldPassword,
        new_password: newPassword,
      });
      toast.success(t("auth.changePasswordSuccess"));
      handleOpenChange(false);
    } catch (err) {
      if (err instanceof CorlinmanApiError) {
        if (err.status === 401) {
          setError(t("auth.changePasswordInvalidOld"));
        } else if (err.status === 422) {
          setError(t("auth.changePasswordWeak", { min: MIN_PASSWORD_LEN }));
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
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        data-testid="change-password-dialog"
        className="max-w-md"
      >
        <DialogHeader>
          <DialogTitle>{t("auth.changePasswordTitle")}</DialogTitle>
          <DialogDescription>
            {t("auth.changePasswordDescription")}
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={onSubmit} className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="cpw-old">
              {t("auth.changePasswordOldLabel")}
            </Label>
            <Input
              id="cpw-old"
              type="password"
              autoComplete="current-password"
              required
              value={oldPassword}
              onChange={(e) => setOldPassword(e.target.value)}
              disabled={submitting}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="cpw-new">
              {t("auth.changePasswordNewLabel")}
            </Label>
            <Input
              id="cpw-new"
              type="password"
              autoComplete="new-password"
              required
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              disabled={submitting}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="cpw-confirm">
              {t("auth.changePasswordConfirmLabel")}
            </Label>
            <Input
              id="cpw-confirm"
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
              data-testid="change-password-error"
            >
              {error}
            </p>
          ) : null}
          <DialogFooter>
            <Button
              type="button"
              variant="ghost"
              onClick={() => handleOpenChange(false)}
              disabled={submitting}
            >
              {t("auth.changePasswordCancel")}
            </Button>
            <Button type="submit" disabled={submitting}>
              {submitting
                ? t("auth.changePasswordSubmitting")
                : t("auth.changePasswordSubmit")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
