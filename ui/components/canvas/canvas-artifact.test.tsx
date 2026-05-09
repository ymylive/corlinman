/**
 * Phase 4 W3 C3 iter 9 — vitest coverage for the Canvas artifact
 * components.
 *
 * Tests run under jsdom + zh-CN (vitest.setup.ts forces the locale)
 * so we anchor assertions on either zh-CN strings or `data-*`
 * attributes that are locale-independent.
 *
 * Covers:
 *   - happy-path render of each artifact kind
 *   - belt-and-braces sanitisation (`<script>` + `on*`)
 *   - warnings footer
 *   - hideMeta hides the header chip
 *   - loading skeleton role + label
 *   - error panel: code-keyed headline, retry callback, raw-source
 *     `<details>` toggle
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import {
  CanvasArtifact,
  stripUnsafeMarkup,
  type RenderedArtifact,
} from "./canvas-artifact";
import { CanvasArtifactLoading } from "./canvas-artifact-loading";
import { CanvasArtifactError } from "./canvas-artifact-error";

afterEach(() => {
  cleanup();
});

const baseArtifact: RenderedArtifact = {
  html_fragment: '<pre class="cn-canvas-code"><code>fn main() {}</code></pre>',
  theme_class: "tp-light",
  content_hash:
    "a".repeat(64),
  render_kind: "code",
  warnings: [],
};

describe("CanvasArtifact", () => {
  it("renders the html fragment inside a labelled figure", () => {
    render(<CanvasArtifact artifact={baseArtifact} />);
    const fig = screen.getByRole("figure");
    expect(fig).toHaveAttribute("data-render-kind", "code");
    expect(fig).toHaveAttribute("data-content-hash", baseArtifact.content_hash);
    // The inner HTML is dropped by `dangerouslySetInnerHTML`; assert via the
    // rendered text node.
    expect(fig.querySelector("pre.cn-canvas-code")).not.toBeNull();
    expect(fig.textContent).toContain("fn main() {}");
  });

  it("propagates theme_class onto the figure", () => {
    const dark: RenderedArtifact = { ...baseArtifact, theme_class: "tp-dark" };
    render(<CanvasArtifact artifact={dark} />);
    expect(screen.getByRole("figure").className).toMatch(/tp-dark/);
  });

  it("strips inline <script> blocks defensively", () => {
    const dirty: RenderedArtifact = {
      ...baseArtifact,
      html_fragment: '<p>ok</p><script>alert(1)</script>',
    };
    render(<CanvasArtifact artifact={dirty} />);
    const fig = screen.getByRole("figure");
    expect(fig.querySelector("script")).toBeNull();
    expect(fig.textContent).toContain("ok");
  });

  it("strips on* event-handler attributes", () => {
    const dirty: RenderedArtifact = {
      ...baseArtifact,
      html_fragment: '<button onclick="alert(1)">x</button>',
    };
    render(<CanvasArtifact artifact={dirty} />);
    const btn = screen.getByRole("figure").querySelector("button");
    expect(btn).not.toBeNull();
    expect(btn?.getAttribute("onclick")).toBeNull();
  });

  it("renders warnings footer when present", () => {
    const warned: RenderedArtifact = {
      ...baseArtifact,
      warnings: ["language `klingon` not recognised; rendered as plain text"],
    };
    render(<CanvasArtifact artifact={warned} />);
    expect(screen.getByText(/klingon/)).toBeInTheDocument();
  });

  it("hides meta header when hideMeta is set", () => {
    render(<CanvasArtifact artifact={baseArtifact} hideMeta />);
    // Short-hash chip should be absent.
    const fig = screen.getByRole("figure");
    expect(fig.querySelector("header")).toBeNull();
  });

  it("renders a sparkline figure with svg passthrough", () => {
    const spark: RenderedArtifact = {
      ...baseArtifact,
      render_kind: "sparkline",
      html_fragment:
        '<svg class="cn-canvas-spark" viewBox="0 0 10 4"><path d="M0 0 L10 4"/></svg>',
    };
    render(<CanvasArtifact artifact={spark} />);
    const fig = screen.getByRole("figure");
    expect(fig.querySelector("svg.cn-canvas-spark")).not.toBeNull();
    expect(fig).toHaveAttribute("data-render-kind", "sparkline");
  });
});

describe("stripUnsafeMarkup", () => {
  it("preserves benign markup byte-for-byte", () => {
    const html = '<pre class="cn-canvas-code"><code>fn main() {}</code></pre>';
    expect(stripUnsafeMarkup(html)).toBe(html);
  });

  it("removes <style> blocks too", () => {
    const dirty = '<p>ok</p><style>p{color:red}</style>';
    const cleaned = stripUnsafeMarkup(dirty);
    expect(cleaned).not.toMatch(/<style/i);
    expect(cleaned).toContain("<p>ok</p>");
  });

  it("rewrites javascript: URIs", () => {
    const dirty = '<a href="javascript:alert(1)">x</a>';
    const cleaned = stripUnsafeMarkup(dirty);
    expect(cleaned).not.toMatch(/javascript:/i);
    expect(cleaned).toMatch(/href="#"/);
  });
});

describe("CanvasArtifactLoading", () => {
  it("exposes a polite live region with the loading label", () => {
    render(<CanvasArtifactLoading />);
    const status = screen.getByRole("status");
    expect(status).toHaveAttribute("aria-live", "polite");
    expect(status.getAttribute("aria-label")).toBeTruthy();
  });

  it("annotates the kind hint via data attr", () => {
    render(<CanvasArtifactLoading kindHint="table" />);
    const status = screen.getByRole("status");
    expect(status).toHaveAttribute("data-render-kind", "table");
  });
});

describe("CanvasArtifactError", () => {
  it("renders the gateway message and a code-keyed headline", () => {
    render(
      <CanvasArtifactError
        code="timeout"
        message="renderer timed out after 5000 ms (kind=Mermaid)"
        artifactKind="mermaid"
      />,
    );
    const alert = screen.getByRole("alert");
    expect(alert).toHaveAttribute("data-error-code", "timeout");
    expect(alert).toHaveAttribute("data-render-kind", "mermaid");
    expect(alert.textContent).toContain("5000");
  });

  it("invokes onRetry when the retry button is clicked", () => {
    const onRetry = vi.fn();
    render(
      <CanvasArtifactError
        code="adapter_error"
        message="adapter rejected"
        onRetry={onRetry}
      />,
    );
    const buttons = screen.getAllByRole("button");
    expect(buttons).toHaveLength(1);
    fireEvent.click(buttons[0]);
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("renders the raw source inside a <details> when supplied", () => {
    render(
      <CanvasArtifactError
        code="adapter_error"
        message="parse error"
        rawSource="graph LR; A-->B"
      />,
    );
    const alert = screen.getByRole("alert");
    const details = alert.querySelector("details");
    expect(details).not.toBeNull();
    expect(details?.textContent).toContain("graph LR; A-->B");
  });

  it("falls back to the generic headline for unknown codes", () => {
    render(<CanvasArtifactError code="generic" message="?" />);
    const alert = screen.getByRole("alert");
    // We don't assert on the specific zh-CN string, only that the
    // alert mounted without throwing and carries the code attr.
    expect(alert).toHaveAttribute("data-error-code", "generic");
  });
});
