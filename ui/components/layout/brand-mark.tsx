import { cn } from "@/lib/utils";

/**
 * Corlinman wordmark. Tidepool treatment: an amber→ember rounded pill with
 * a soft inner highlight + outer glow. Used in sidebar + login + palette.
 */
export function BrandMark({
  compact = false,
  className,
}: {
  compact?: boolean;
  className?: string;
}) {
  return (
    <div className={cn("flex items-center gap-2", className)}>
      <span
        className="relative inline-flex h-[30px] w-[30px] items-center justify-center overflow-hidden rounded-[9px]"
        style={{
          background:
            "linear-gradient(135deg, var(--tp-amber), var(--tp-ember) 70%)",
          boxShadow:
            "inset 0 1px 0 rgba(255,255,255,0.4), 0 0 16px -4px var(--tp-amber-glow)",
        }}
      >
        {/* inner glass bead — gives the mark a subtle "lit from above" feel */}
        <span
          aria-hidden
          className="pointer-events-none absolute inset-[4px] rounded-[6px]"
          style={{
            background:
              "radial-gradient(circle at 30% 30%, rgba(255,255,255,0.8), transparent 50%), rgba(255,255,255,0.12)",
          }}
        />
      </span>
      {compact ? null : (
        <div className="min-w-0 flex-col leading-tight">
          <div className="text-[14.5px] font-semibold tracking-[-0.015em] text-tp-ink">
            corlinman
          </div>
          <div className="font-mono text-[10.5px] text-tp-ink-3">
            v0.3.0 · batch 1–5
          </div>
        </div>
      )}
    </div>
  );
}
