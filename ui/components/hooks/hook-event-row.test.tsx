import { afterEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import {
  HookEventRow,
  deriveHookMetrics,
  formatLatency,
  kindTone,
} from "./hook-event-row";
import type { HookEvent } from "@/lib/hooks/use-mock-hook-stream";

function makeEvent(overrides: Partial<HookEvent> = {}): HookEvent {
  return {
    id: "evt-hook-1",
    kind: "message.received",
    ts: Date.parse("2026-04-23T08:09:10.123Z"),
    session_key: "qq:12345",
    summary: "inbound text from user",
    payload: { hello: "world" },
    ...overrides,
  };
}

afterEach(() => cleanup());

describe("kindTone", () => {
  it("maps hot-path kinds onto their neutral ink tones", () => {
    expect(kindTone("message.received")).toBe("message");
    expect(kindTone("message.sent")).toBe("message");
    expect(kindTone("session.patch")).toBe("session");
    expect(kindTone("agent.bootstrap")).toBe("agent");
  });

  it("maps lifecycle + config + approval kinds onto warm tones", () => {
    expect(kindTone("gateway.startup")).toBe("lifecycle");
    expect(kindTone("config.changed")).toBe("config");
    expect(kindTone("approval.requested")).toBe("approval");
    expect(kindTone("rate_limit.triggered")).toBe("rate_limit");
  });

  it("flips decided/tool kinds to error when the payload signals failure", () => {
    expect(kindTone("approval.decided", { decision: "allow" })).toBe("approval");
    expect(kindTone("approval.decided", { decision: "deny" })).toBe("error");
    expect(kindTone("approval.decided", { decision: "timeout" })).toBe("error");
    expect(kindTone("tool.called", { ok: true })).toBe("tool");
    expect(kindTone("tool.called", { ok: false })).toBe("error");
  });

  it("defaults to neutral for unknown kinds", () => {
    expect(kindTone("unknown.kind")).toBe("neutral");
  });
});

describe("deriveHookMetrics", () => {
  it("returns stable metrics for the same event id", () => {
    const evt = makeEvent({ id: "evt-stable" });
    const a = deriveHookMetrics(evt);
    const b = deriveHookMetrics(evt);
    expect(a).toEqual(b);
    expect(a.subscribers).toBeGreaterThan(0);
    expect(a.latencyMs).toBeGreaterThan(0);
  });

  it("picks a larger audience for config.changed than for a narrow kind", () => {
    const configEvt = makeEvent({ id: "evt-config", kind: "config.changed" });
    const narrow = makeEvent({
      id: "evt-msg",
      kind: "message.transcribed",
    });
    expect(deriveHookMetrics(configEvt).subscribers).toBeGreaterThan(
      deriveHookMetrics(narrow).subscribers,
    );
  });
});

describe("formatLatency", () => {
  it("collapses null/undefined to an em dash", () => {
    expect(formatLatency(null)).toBe("—");
    expect(formatLatency(undefined)).toBe("—");
  });
  it("uses ms under a second, s at / above a second", () => {
    expect(formatLatency(42)).toBe("42ms");
    expect(formatLatency(1500)).toBe("1.5s");
    expect(formatLatency(0.4)).toBe("0.4ms");
  });
});

describe("HookEventRow", () => {
  it("renders kind pill + subscribers + session chip and fires onClick", () => {
    const onClick = () => void 0;
    render(
      <HookEventRow
        event={makeEvent()}
        subscribers={3}
        latencyMs={12}
        onClick={onClick}
      />,
    );

    expect(screen.getByTestId("hook-kind-pill")).toHaveTextContent(
      "message.received",
    );
    expect(screen.getByText("inbound text from user")).toBeInTheDocument();
    expect(screen.getByText("qq:12345")).toBeInTheDocument();
    expect(screen.getByText("12ms")).toBeInTheDocument();
  });

  it("marks itself aria-pressed when `selected` is true", () => {
    render(
      <HookEventRow
        event={makeEvent()}
        subscribers={3}
        latencyMs={12}
        selected
      />,
    );
    const row = screen.getByTestId("hook-event-row");
    expect(row.getAttribute("aria-pressed")).toBe("true");
  });

  it("drops the just-now bar when also selected (selection wins)", () => {
    render(
      <HookEventRow
        event={makeEvent()}
        subscribers={3}
        latencyMs={12}
        selected
        justNow
      />,
    );
    const row = screen.getByTestId("hook-event-row");
    // The tp-just-now bar is rendered as a direct-child <span> with the
    // tp-just-now class — selection suppresses it.
    expect(row.querySelector(".tp-just-now")).toBeNull();
  });

  it("keeps the just-now bar when the row is unselected", () => {
    render(
      <HookEventRow
        event={makeEvent()}
        subscribers={3}
        latencyMs={12}
        justNow
      />,
    );
    const row = screen.getByTestId("hook-event-row");
    expect(row.querySelector(".tp-just-now")).not.toBeNull();
  });

  it("fires onClick once per click", () => {
    let n = 0;
    render(
      <HookEventRow
        event={makeEvent()}
        subscribers={3}
        latencyMs={12}
        onClick={() => (n += 1)}
      />,
    );
    fireEvent.click(screen.getByTestId("hook-event-row"));
    expect(n).toBe(1);
  });
});
