"use client";

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

export interface TraceIdBadgeProps {
  /** Value of the `x-request-id` header surfaced by corlinman-gateway. */
  traceId: string | null | undefined;
  className?: string;
  /** Truncate the id to N chars for dense tables. Default 12. */
  maxChars?: number;
}

/**
 * Displays the gateway-issued `request_id` (see plan §9 observability).
 * TODO(M6): click-through to logs view pre-filtered by trace_id.
 */
export function TraceIdBadge({
  traceId,
  className,
  maxChars = 12,
}: TraceIdBadgeProps) {
  if (!traceId) return null;
  const shown =
    traceId.length > maxChars ? `${traceId.slice(0, maxChars)}…` : traceId;
  return (
    <Badge
      variant="outline"
      className={cn("font-mono text-[10px] tracking-tight", className)}
      title={traceId}
    >
      trace {shown}
    </Badge>
  );
}
