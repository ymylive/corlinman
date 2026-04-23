"use client";

import { useTranslation } from "react-i18next";
import { StatChip } from "@/components/ui/stat-chip";

/**
 * Four-stat row for the QQ channel page: Inbound · Chats · Keywords · Status.
 *
 * Until a dedicated /admin/channels/qq/metrics endpoint lands we derive
 * counts from the QqStatus snapshot. The "Inbound/24h" slot is a rolling
 * view of `recent_messages.length` (capped by the gateway at 50). When
 * the channel is unreachable every value collapses to `—`.
 */

const INBOUND_SPARK =
  "M0 26 L30 24 L60 20 L90 24 L120 16 L150 22 L180 14 L210 18 L240 10 L270 14 L300 8 L300 36 L0 36 Z";
const CHATS_SPARK =
  "M0 22 L30 22 L60 20 L90 22 L120 18 L150 20 L180 18 L210 20 L240 16 L270 18 L300 16 L300 36 L0 36 Z";
const KEYWORDS_SPARK =
  "M0 18 L30 20 L60 16 L90 22 L120 14 L150 20 L180 18 L210 22 L240 16 L270 20 L300 14 L300 36 L0 36 Z";
const THROTTLE_SPARK =
  "M0 30 L30 28 L60 30 L90 26 L120 28 L150 24 L180 26 L210 22 L240 24 L270 20 L300 22 L300 36 L0 36 Z";

export interface QqStatsRowProps {
  inbound: number;
  chats: number;
  keywords: number;
  throttled: number;
  /** Collapses values to `—` when false. */
  live: boolean;
}

export function QqStatsRow({
  inbound,
  chats,
  keywords,
  throttled,
  live,
}: QqStatsRowProps) {
  const { t } = useTranslation();
  const offlineFoot = t("channels.qq.tp.statOfflineFoot");

  return (
    <section className="grid grid-cols-1 gap-3.5 md:grid-cols-2 xl:grid-cols-4">
      <StatChip
        variant="primary"
        live={live}
        label={t("channels.qq.tp.statInbound")}
        value={live ? inbound : "—"}
        foot={live ? t("channels.qq.tp.statFootInbound") : offlineFoot}
        sparkPath={INBOUND_SPARK}
        sparkTone="amber"
      />
      <StatChip
        label={t("channels.qq.tp.statChats")}
        value={live ? chats : "—"}
        foot={live ? t("channels.qq.tp.statFootChats") : offlineFoot}
        sparkPath={CHATS_SPARK}
        sparkTone="ember"
      />
      <StatChip
        label={t("channels.qq.tp.statKeywords")}
        value={live ? keywords : "—"}
        foot={live ? t("channels.qq.tp.statFootKeywords") : offlineFoot}
        sparkPath={KEYWORDS_SPARK}
        sparkTone="peach"
      />
      <StatChip
        label={t("channels.qq.tp.statThrottled")}
        value={live ? throttled : "—"}
        delta={
          live
            ? throttled === 0
              ? { label: t("channels.qq.tp.caughtUp"), tone: "up" }
              : { label: t("channels.qq.tp.needsAttention"), tone: "down" }
            : undefined
        }
        foot={live ? t("channels.qq.tp.statFootThrottled") : offlineFoot}
        sparkPath={THROTTLE_SPARK}
        sparkTone="ember"
      />
    </section>
  );
}

export default QqStatsRow;
