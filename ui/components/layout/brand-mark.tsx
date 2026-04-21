import { cn } from "@/lib/utils";

/** Corlinman wordmark with a small accent square. Used in sidebar + login. */
export function BrandMark({
  compact = false,
  className,
}: {
  compact?: boolean;
  className?: string;
}) {
  return (
    <div className={cn("flex items-center gap-2", className)}>
      <span className="relative inline-flex h-6 w-6 items-center justify-center rounded-md bg-primary text-primary-foreground shadow-[inset_0_0_0_1px_hsl(var(--primary)/0.3)]">
        <span className="font-mono text-[11px] font-bold">c</span>
      </span>
      {compact ? null : (
        <span className="text-sm font-semibold tracking-tight">corlinman</span>
      )}
    </div>
  );
}
