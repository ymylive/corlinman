"use client";

import * as React from "react";
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
 *
 * Approve-with-reason is not implemented this round because the UX value
 * is small and the spec flagged it as optional; deny is where audit
 * context actually matters.
 */
export interface DenyReasonDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Human label for the action — "1 条" or "3 条" etc. */
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
          <DialogTitle>拒绝 {targetLabel}</DialogTitle>
          <DialogDescription>
            拒绝理由将随决定一起持久化，至少 {MIN_REASON} 个字符。
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-2">
          <Label htmlFor="deny-reason">Reason</Label>
          <Input
            id="deny-reason"
            value={reason}
            autoFocus
            onChange={(e) => setReason(e.target.value)}
            placeholder="eg. 命中黑名单路径 / 参数不安全"
            disabled={submitting}
          />
        </div>
        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={submitting}
          >
            取消
          </Button>
          <Button
            variant="destructive"
            onClick={() => onConfirm(trimmed)}
            disabled={tooShort || submitting}
          >
            拒绝
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
