import { describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { DenyReasonDialog } from "../DenyReasonDialog";

describe("DenyReasonDialog", () => {
  it("disables confirm until reason reaches minimum length", () => {
    const onConfirm = vi.fn();
    render(
      <DenyReasonDialog
        open
        onOpenChange={() => {}}
        targetLabel="1 条"
        onConfirm={onConfirm}
      />,
    );

    const input = screen.getByLabelText("Reason") as HTMLInputElement;
    const confirmBtn = screen.getByRole("button", { name: "拒绝" });
    expect(confirmBtn).toBeDisabled();

    // Below 5-char threshold.
    fireEvent.change(input, { target: { value: "nope" } });
    expect(confirmBtn).toBeDisabled();

    fireEvent.change(input, { target: { value: "unsafe path" } });
    expect(confirmBtn).toBeEnabled();

    fireEvent.click(confirmBtn);
    expect(onConfirm).toHaveBeenCalledWith("unsafe path");
  });
});
