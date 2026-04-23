"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";
import { GlassPanel } from "@/components/ui/glass-panel";
import { LogRow } from "@/components/ui/log-row";
import type { QqRecentMessage } from "./qq-util";
import { formatTsShort } from "./qq-util";

/**
 * Recent messages feed — dense `<LogRow>` per entry. The "subsystem" column
 * is repurposed for `chat_id · sender` so at-a-glance scanning still works
 * without introducing a new primitive.
 */

export interface QqMessagesPanelProps {
  messages: QqRecentMessage[];
  offline: boolean;
}

const MAX_ROWS = 20;

export function QqMessagesPanel({ messages, offline }: QqMessagesPanelProps) {
  const { t } = useTranslation();

  const rows = React.useMemo(
    () => messages.slice(0, MAX_ROWS),
    [messages],
  );

  return (
    <GlassPanel
      variant="soft"
      as="section"
      className="flex flex-col overflow-hidden"
    >
      <div className="flex items-center justify-between border-b border-tp-glass-edge px-4 py-3">
        <div className="flex items-center gap-2">
          <h2 className="text-[13px] font-medium text-tp-ink">
            {t("channels.recentMessages")}
          </h2>
          <span className="font-mono text-[10.5px] text-tp-ink-4">
            {offline ? "—" : `${rows.length}/${MAX_ROWS}`}
          </span>
        </div>
      </div>
      <div className="max-h-[360px] overflow-auto">
        {offline ? (
          <p className={emptyCls}>{t("channels.qq.tp.messagesOffline")}</p>
        ) : rows.length === 0 ? (
          <p className={emptyCls}>{t("channels.noMessages")}</p>
        ) : (
          <ul className="flex flex-col">
            {rows.map((m, i) => (
              <li key={i}>
                <LogRow
                  ts={formatTsShort(m.ts)}
                  severity="info"
                  subsystem={
                    [m.chatId, m.sender].filter(Boolean).join(" · ") ||
                    t("channels.qq.tp.messagesUnknownSender")
                  }
                  message={m.preview}
                  title={m.preview}
                  tabIndex={-1}
                  // LogRow is a button; leave it non-interactive here by
                  // disabling focusing (tabIndex=-1 above) and keeping the
                  // row static — this is a read-only feed.
                />
              </li>
            ))}
          </ul>
        )}
      </div>
    </GlassPanel>
  );
}

const emptyCls = cn(
  "px-4 py-8 text-center text-[12.5px] text-tp-ink-3",
);

export default QqMessagesPanel;
