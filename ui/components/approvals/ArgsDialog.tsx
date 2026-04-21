"use client";

import { useTranslation } from "react-i18next";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import type { Approval } from "./types";

/** Full-args Dialog.
 *
 * Reason we stuck with `<pre>` + `JSON.stringify` instead of pulling in
 * react-syntax-highlighter: the args are short (tool call payloads, not
 * logs) and an extra ~40 KB bundle for color is not worth it. If that
 * changes, the rendering is isolated in this one component.
 */
function formatTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

function prettifyJson(raw: string): string {
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    // Non-JSON payload (base64-encoded bytes) — render verbatim.
    return raw;
  }
}

export function ArgsDialog({ approval }: { approval: Approval }) {
  const { t } = useTranslation();
  return (
    <Dialog>
      <DialogTrigger asChild>
        <Button size="sm" variant="outline">
          {t("approvals.viewArgs")}
        </Button>
      </DialogTrigger>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>
            {approval.plugin}.{approval.tool}
          </DialogTitle>
          <DialogDescription asChild>
            <div className="space-y-1 text-xs">
              <div>
                {t("approvals.argsSessionKey")}:{" "}
                <span className="font-mono">
                  {approval.session_key || t("approvals.emptyValue")}
                </span>
              </div>
              <div>
                {t("approvals.argsRequestedAt")}:{" "}
                <span className="font-mono">
                  {formatTime(approval.requested_at)}
                </span>
              </div>
              {approval.decided_at ? (
                <div>
                  {t("approvals.argsDecidedAt")}:{" "}
                  <span className="font-mono">
                    {formatTime(approval.decided_at)}
                  </span>{" "}
                  — {approval.decision ?? "?"}
                </div>
              ) : null}
              {/* TODO(S5+): render approved_by once the Rust `ApprovalOut`
                  exposes the operator identity (currently not tracked). */}
            </div>
          </DialogDescription>
        </DialogHeader>
        <pre className="max-h-96 overflow-auto rounded-md bg-muted p-3 font-mono text-xs">
          {prettifyJson(approval.args_json)}
        </pre>
      </DialogContent>
    </Dialog>
  );
}
