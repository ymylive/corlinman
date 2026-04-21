"use client";

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Play } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  fetchSchedulerJobs,
  fetchSchedulerHistory,
  triggerSchedulerJob,
  type SchedulerJob,
  type SchedulerHistory,
} from "@/lib/api";

/**
 * Scheduler admin page. Job table with a live next-trigger countdown that
 * ticks every second. Row-click opens the history modal. Trigger button
 * dispatches a POST and surfaces the resulting history entry.
 */
export default function SchedulerPage() {
  const qc = useQueryClient();
  const jobs = useQuery<SchedulerJob[]>({
    queryKey: ["admin", "scheduler", "jobs"],
    queryFn: fetchSchedulerJobs,
    refetchInterval: 60_000,
  });
  const history = useQuery<SchedulerHistory[]>({
    queryKey: ["admin", "scheduler", "history"],
    queryFn: fetchSchedulerHistory,
    refetchInterval: 15_000,
  });

  const [historyJob, setHistoryJob] = React.useState<string | null>(null);

  const triggerMutation = useMutation({
    mutationFn: (name: string) => triggerSchedulerJob(name),
    onSuccess: (_, name) => {
      toast.success(`Triggered "${name}" — see history`);
      qc.invalidateQueries({ queryKey: ["admin", "scheduler", "history"] });
    },
    onError: (err, name) => {
      const msg = err instanceof Error ? err.message : String(err);
      toast.warning(`"${name}" trigger: ${msg}`);
      qc.invalidateQueries({ queryKey: ["admin", "scheduler", "history"] });
    },
  });

  const scopedHistory = React.useMemo(() => {
    if (!historyJob || !history.data) return [];
    return history.data.filter((h) => h.job === historyJob);
  }, [history.data, historyJob]);

  return (
    <>
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">Scheduler</h1>
        <p className="text-sm text-muted-foreground">
          `[[scheduler.jobs]]` snapshot. Cron runtime lands in M7; trigger is
          recorded but a 501 means it was a dry run.
        </p>
      </header>

      <section className="overflow-hidden rounded-lg border border-border bg-panel">
        <Table>
          <TableHeader>
            <TableRow className="border-b border-border hover:bg-transparent">
              <TableHead className="pl-4">Name</TableHead>
              <TableHead>Cron</TableHead>
              <TableHead>TZ</TableHead>
              <TableHead>Action</TableHead>
              <TableHead>Next fire</TableHead>
              <TableHead>Last status</TableHead>
              <TableHead className="w-32"></TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {jobs.isPending ? (
              <TableRow>
                <TableCell colSpan={7} className="p-4">
                  <Skeleton className="h-5 w-full" />
                </TableCell>
              </TableRow>
            ) : jobs.data && jobs.data.length === 0 ? (
              <TableRow>
                <TableCell
                  colSpan={7}
                  className="py-10 text-center text-sm text-muted-foreground"
                >
                  No jobs configured.
                </TableCell>
              </TableRow>
            ) : (
              jobs.data?.map((j) => (
                <TableRow
                  key={j.name}
                  className="group border-b border-border transition-colors hover:bg-accent/30"
                  onClick={() => setHistoryJob(j.name)}
                  style={{ cursor: "pointer" }}
                >
                  <TableCell className="pl-4 font-medium">{j.name}</TableCell>
                  <TableCell className="font-mono text-xs">{j.cron}</TableCell>
                  <TableCell className="font-mono text-xs text-muted-foreground">
                    {j.timezone ?? "utc"}
                  </TableCell>
                  <TableCell>
                    <Badge variant="outline" className="font-mono text-[10px]">
                      {j.action_kind}
                    </Badge>
                  </TableCell>
                  <TableCell>
                    <Countdown iso={j.next_fire_at} />
                  </TableCell>
                  <TableCell>
                    <StatusLabel status={j.last_status} />
                  </TableCell>
                  <TableCell>
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={triggerMutation.isPending}
                      onClick={(e) => {
                        e.stopPropagation();
                        triggerMutation.mutate(j.name);
                      }}
                      data-testid={`scheduler-trigger-${j.name}`}
                    >
                      <Play className="h-3 w-3" />
                      Trigger
                    </Button>
                  </TableCell>
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </section>

      <Dialog
        open={historyJob !== null}
        onOpenChange={(v) => !v && setHistoryJob(null)}
      >
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>
              History
              {historyJob ? (
                <span className="ml-2 font-mono text-xs text-muted-foreground">
                  {historyJob}
                </span>
              ) : null}
            </DialogTitle>
          </DialogHeader>
          <div className="max-h-96 overflow-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>At</TableHead>
                  <TableHead>Source</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Message</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {scopedHistory.length === 0 ? (
                  <TableRow>
                    <TableCell
                      colSpan={4}
                      className="py-4 text-center text-sm text-muted-foreground"
                    >
                      no history yet
                    </TableCell>
                  </TableRow>
                ) : (
                  scopedHistory.map((h, i) => (
                    <TableRow key={`${h.at}-${i}`}>
                      <TableCell className="font-mono text-xs">{h.at}</TableCell>
                      <TableCell className="font-mono text-xs">
                        {h.source}
                      </TableCell>
                      <TableCell>
                        <StatusLabel status={h.status} />
                      </TableCell>
                      <TableCell className="text-xs text-muted-foreground">
                        {h.message}
                      </TableCell>
                    </TableRow>
                  ))
                )}
              </TableBody>
            </Table>
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}

/** Live countdown to `iso`. Updates every second. */
function Countdown({ iso }: { iso: string | null }) {
  const [now, setNow] = React.useState(() => Date.now());
  React.useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);
  if (!iso) return <span className="font-mono text-xs text-muted-foreground">—</span>;
  const then = new Date(iso).getTime();
  const delta = then - now;
  if (isNaN(then))
    return <span className="font-mono text-xs text-muted-foreground">{iso}</span>;
  if (delta <= 0) {
    return (
      <span className="font-mono text-xs text-warn">due · {iso.slice(11, 19)}</span>
    );
  }
  const s = Math.floor(delta / 1000);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = s % 60;
  return (
    <span className="font-mono text-xs tabular-nums">
      {h > 0 ? `${h}h ` : ""}
      {m.toString().padStart(2, "0")}m {ss.toString().padStart(2, "0")}s
    </span>
  );
}

function StatusLabel({ status }: { status: string | null }) {
  if (!status) return <span className="font-mono text-xs text-muted-foreground">—</span>;
  const s = status.toLowerCase();
  const tone =
    s.includes("ok") || s.includes("success")
      ? "text-ok"
      : s.includes("err") || s.includes("fail")
        ? "text-err"
        : "text-muted-foreground";
  return <span className={`font-mono text-xs ${tone}`}>{status}</span>;
}
