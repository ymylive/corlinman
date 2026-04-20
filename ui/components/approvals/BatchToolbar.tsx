"use client";

import { Button } from "@/components/ui/button";

/** Batch-action toolbar that lights up when the operator has selection.
 *
 * Confirm flow intentionally uses the browser-native `confirm()` (invoked
 * by the parent, not here) instead of a shadcn AlertDialog — approved in
 * scope review: it's one line, accessible, and skips another Radix dep.
 */
export interface BatchToolbarProps {
  selectedCount: number;
  onApproveAll: () => void;
  onDenyAll: () => void;
  onClear: () => void;
  disabled?: boolean;
}

export function BatchToolbar({
  selectedCount,
  onApproveAll,
  onDenyAll,
  onClear,
  disabled = false,
}: BatchToolbarProps) {
  if (selectedCount === 0) return null;
  return (
    <div
      role="region"
      aria-label="批量操作"
      className="flex items-center gap-2 rounded-md border border-border bg-muted/40 px-3 py-2 text-sm"
    >
      <span className="font-medium">已选 {selectedCount} 条</span>
      <div className="flex-1" />
      <Button
        size="sm"
        onClick={onApproveAll}
        disabled={disabled}
      >
        批量 Approve
      </Button>
      <Button
        size="sm"
        variant="destructive"
        onClick={onDenyAll}
        disabled={disabled}
      >
        批量 Deny
      </Button>
      <Button size="sm" variant="ghost" onClick={onClear} disabled={disabled}>
        清除
      </Button>
    </div>
  );
}
