"use client";

import { useTranslation } from "react-i18next";
import { Play } from "lucide-react";

import { Button } from "@/components/ui/button";
import { TableCell, TableRow } from "@/components/ui/table";
import type { SessionSummary } from "@/lib/api/sessions";

/**
 * Single row in the sessions list — extracted so the page module stays
 * focused on data fetching + scaffolding. Mirrors the shape of the rows on
 * `/admin/agents` (mono-font key, light text for secondary metadata, an
 * action button anchored to the right).
 *
 * `last_message_at` is unix milliseconds (per Agent A's wire contract). We
 * format with `new Date(ms).toLocaleString()` so the operator's locale is
 * honored automatically.
 */

interface SessionRowProps {
  session: SessionSummary;
  onReplay: (session: SessionSummary) => void;
}

function formatTime(ms: number): string {
  const d = new Date(ms);
  if (Number.isNaN(d.getTime())) return String(ms);
  return d.toLocaleString();
}

export function SessionRow({ session, onReplay }: SessionRowProps) {
  const { t } = useTranslation();
  return (
    <TableRow
      className="border-b border-tp-glass-edge transition-colors hover:bg-tp-glass-inner-hover"
      data-testid={`session-row-${session.session_key}`}
    >
      <TableCell className="pl-4 font-mono text-[13px] text-tp-ink">
        {session.session_key}
      </TableCell>
      <TableCell className="font-mono text-xs text-tp-ink-2">
        {session.message_count}
      </TableCell>
      <TableCell className="text-xs text-tp-ink-3">
        <time dateTime={new Date(session.last_message_at).toISOString()}>
          {formatTime(session.last_message_at)}
        </time>
      </TableCell>
      <TableCell className="pr-4 text-right">
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => onReplay(session)}
          data-testid={`session-replay-${session.session_key}`}
          aria-label={`${t("sessions.replay")} ${session.session_key}`}
        >
          <Play className="h-3.5 w-3.5" aria-hidden="true" />
          {t("sessions.replay")}
        </Button>
      </TableCell>
    </TableRow>
  );
}
