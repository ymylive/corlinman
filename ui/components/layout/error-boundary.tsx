"use client";

import * as React from "react";
import { AlertTriangle, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";

interface ErrorBoundaryProps {
  children: React.ReactNode;
}

interface ErrorBoundaryState {
  error: Error | null;
}

export class PageErrorBoundary extends React.Component<
  ErrorBoundaryProps,
  ErrorBoundaryState
> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo): void {
    // Surface in the browser console for triage; the panel itself shows
    // the shortened stack.
    // eslint-disable-next-line no-console
    console.error("page crashed", error, info);
  }

  reset = () => this.setState({ error: null });

  render() {
    if (this.state.error) {
      return (
        <div className="flex min-h-[40vh] items-center justify-center p-6">
          <div className="max-w-xl space-y-3 rounded-md border border-destructive/40 bg-destructive/5 p-5 text-sm">
            <div className="flex items-center gap-2 font-medium text-destructive">
              <AlertTriangle className="h-4 w-4" />
              This page hit a runtime error
            </div>
            <p className="text-muted-foreground">
              The rest of the app still works. Try reloading or navigating
              away and back. If the issue persists, the error message is
              below (and in the browser console).
            </p>
            <pre className="max-h-60 overflow-auto rounded bg-background/60 p-3 font-mono text-[11px] text-foreground/80">
              {this.state.error.message}
              {this.state.error.stack
                ? "\n\n" + this.state.error.stack.split("\n").slice(0, 8).join("\n")
                : null}
            </pre>
            <div className="flex items-center gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={this.reset}
                className="gap-1"
              >
                <RefreshCw className="h-3.5 w-3.5" />
                Try again
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => {
                  if (typeof window !== "undefined") window.location.reload();
                }}
              >
                Hard reload
              </Button>
            </div>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
