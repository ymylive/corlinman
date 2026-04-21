"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

/** Controlled deny-with-reason dialog.
 *
 * Required minimum length = 5 chars, matching the copy in the task spec.
 * Reason travels to the Rust `DecideBody { approve: false, reason }` which
 * is stored alongside the decision (see `approvals.rs::decide_approval`).
 */
export interface DenyReasonDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Human label for the action — already localized by the caller. */
  targetLabel: string;
  onConfirm: (reason: string) => void;
  submitting?: boolean;
}

const MIN_REASON = 5;

export function DenyReasonDialog({
  open,
  onOpenChange,
  targetLabel,
  onConfirm,
  submitting = false,
}: DenyReasonDialogProps) {
  const { t } = useTranslation();
  const [reason, setReason] = React.useState("");

  React.useEffect(() => {
    if (!open) setReason("");
  }, [open]);

  const trimmed = reason.trim();
  const tooShort = trimmed.length < MIN_REASON;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            {t("approvals.denyDialogTitle", { target: targetLabel })}
          </DialogTitle>
          <DialogDescription>
            {t("approvals.denyDialogBody", { min: MIN_REASON })}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-2">
          <Label htmlFor="deny-reason">{t("approvals.denyReasonLabel")}</Label>
          <Input
            id="deny-reason"
            value={reason}
            autoFocus
            onChange={(e) => setReason(e.target.value)}
            placeholder={t("approvals.denyReasonPlaceholder")}
            disabled={submitting}
          />
        </div>
        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={submitting}
          >
            {t("approvals.denyCancel")}
          </Button>
          <Button
            variant="destructive"
            onClick={() => onConfirm(trimmed)}
            disabled={tooShort || submitting}
          >
            {t("approvals.denyConfirm")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
