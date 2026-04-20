"use client";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

interface TodoCardProps {
  title: string;
  description: string;
  /** Milestone that delivers this surface (e.g. "M6", "M4"). */
  milestone?: string;
  /** Short list of integration points that still need wiring. */
  todos?: string[];
}

/**
 * Consistent placeholder card used by every admin page at M0. Each page
 * replaces this with real data during the milestone listed below.
 */
export function TodoCard({ title, description, milestone, todos }: TodoCardProps) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center justify-between gap-2">
          <span>{title}</span>
          {milestone ? (
            <span className="rounded bg-muted px-2 py-0.5 text-xs font-mono text-muted-foreground">
              {milestone}
            </span>
          ) : null}
        </CardTitle>
        <CardDescription>{description}</CardDescription>
      </CardHeader>
      <CardContent>
        {todos && todos.length > 0 ? (
          <ul className="list-disc space-y-1 pl-5 text-sm text-muted-foreground">
            {todos.map((t) => (
              <li key={t}>{t}</li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-muted-foreground">
            占位：此面板将在对应里程碑接入 gateway 的 /admin/* 接口。
          </p>
        )}
      </CardContent>
      <CardFooter className="gap-2">
        <Button variant="outline" size="sm" disabled>
          刷新
        </Button>
        <Button size="sm" disabled>
          查看详情
        </Button>
      </CardFooter>
    </Card>
  );
}
