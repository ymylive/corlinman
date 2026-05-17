/**
 * Tests for the two-stage onboard ModelPickerDialog (Wave 2.1).
 *
 * Covers:
 *   1. Stage 1 renders provider rows; clicking a row advances to Stage 2.
 *   2. Search filters the visible providers in Stage 1.
 *   3. Stage 2 lists provider-scoped models; search filters them.
 *   4. Selecting a model fires `onPick` with `{channel_id, model}` and
 *      closes the dialog (onOpenChange(false)).
 *   5. Empty-state copy when there are no providers / no model matches.
 *
 * Locale is zh-CN per the rest of the UI suite — see vitest.setup.ts.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, within } from "@testing-library/react";

import { ModelPickerDialog } from "./model-picker-dialog";
import type { NewapiChannel } from "@/lib/api";

const PROVIDERS: NewapiChannel[] = [
  {
    id: 1,
    name: "OpenAI",
    type: 1,
    status: 1,
    models: "gpt-4o, gpt-4o-mini, gpt-3.5-turbo",
  },
  {
    id: 2,
    name: "Anthropic",
    type: 1,
    status: 1,
    models: "claude-3-opus, claude-3-sonnet",
  },
];

describe("ModelPickerDialog", () => {
  let onPick: ReturnType<typeof vi.fn>;
  let onOpenChange: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    onPick = vi.fn();
    onOpenChange = vi.fn();
  });

  it("renders provider rows on open (Stage 1)", () => {
    render(
      <ModelPickerDialog
        open
        onOpenChange={onOpenChange}
        providers={PROVIDERS}
        onPick={onPick}
        kind="llm"
      />,
    );

    expect(
      screen.getByTestId("model-picker-provider-1"),
    ).toHaveTextContent("OpenAI");
    expect(
      screen.getByTestId("model-picker-provider-2"),
    ).toHaveTextContent("Anthropic");
  });

  it("filters providers when the user types in search", () => {
    render(
      <ModelPickerDialog
        open
        onOpenChange={onOpenChange}
        providers={PROVIDERS}
        onPick={onPick}
        kind="llm"
      />,
    );

    fireEvent.change(screen.getByTestId("model-picker-search"), {
      target: { value: "claude" },
    });

    expect(screen.queryByTestId("model-picker-provider-1")).toBeNull();
    expect(
      screen.getByTestId("model-picker-provider-2"),
    ).toBeInTheDocument();
  });

  it("clicking a provider advances to Stage 2 and lists its models", () => {
    render(
      <ModelPickerDialog
        open
        onOpenChange={onOpenChange}
        providers={PROVIDERS}
        onPick={onPick}
        kind="llm"
      />,
    );

    fireEvent.click(screen.getByTestId("model-picker-provider-1"));

    const list = screen.getByTestId("model-picker-list");
    expect(within(list).getByText("gpt-4o")).toBeInTheDocument();
    expect(within(list).getByText("gpt-4o-mini")).toBeInTheDocument();
    expect(within(list).getByText("gpt-3.5-turbo")).toBeInTheDocument();
    // The Anthropic models must NOT leak into the OpenAI model list.
    expect(within(list).queryByText("claude-3-opus")).toBeNull();
  });

  it("filters models inside Stage 2", () => {
    render(
      <ModelPickerDialog
        open
        onOpenChange={onOpenChange}
        providers={PROVIDERS}
        onPick={onPick}
        kind="llm"
      />,
    );

    fireEvent.click(screen.getByTestId("model-picker-provider-1"));
    fireEvent.change(screen.getByTestId("model-picker-search"), {
      target: { value: "mini" },
    });

    expect(screen.getByTestId("model-picker-model-gpt-4o-mini")).toBeInTheDocument();
    expect(screen.queryByTestId("model-picker-model-gpt-4o")).toBeNull();
  });

  it("selecting a model fires onPick and closes the dialog", () => {
    render(
      <ModelPickerDialog
        open
        onOpenChange={onOpenChange}
        providers={PROVIDERS}
        onPick={onPick}
        kind="llm"
      />,
    );

    fireEvent.click(screen.getByTestId("model-picker-provider-2"));
    fireEvent.click(
      screen.getByTestId("model-picker-model-claude-3-sonnet"),
    );

    expect(onPick).toHaveBeenCalledWith({
      channel_id: 2,
      model: "claude-3-sonnet",
    });
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("shows the no-providers empty state when the list is empty", () => {
    render(
      <ModelPickerDialog
        open
        onOpenChange={onOpenChange}
        providers={[]}
        onPick={onPick}
        kind="embedding"
      />,
    );

    expect(screen.getByText("没有可用的 provider")).toBeInTheDocument();
  });

  it("shows the no-models empty state when filter has no matches", () => {
    render(
      <ModelPickerDialog
        open
        onOpenChange={onOpenChange}
        providers={PROVIDERS}
        onPick={onPick}
        kind="llm"
      />,
    );

    fireEvent.click(screen.getByTestId("model-picker-provider-1"));
    fireEvent.change(screen.getByTestId("model-picker-search"), {
      target: { value: "nonexistent-zzz" },
    });

    expect(screen.getByText("无匹配模型")).toBeInTheDocument();
  });
});
