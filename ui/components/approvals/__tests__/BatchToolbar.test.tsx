import { describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { BatchToolbar } from "../BatchToolbar";

describe("BatchToolbar", () => {
  it("stays hidden when nothing is selected", () => {
    const { container } = render(
      <BatchToolbar
        selectedCount={0}
        onApproveAll={() => {}}
        onDenyAll={() => {}}
        onClear={() => {}}
      />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("fires callbacks for approve / deny / clear", () => {
    const onApproveAll = vi.fn();
    const onDenyAll = vi.fn();
    const onClear = vi.fn();
    render(
      <BatchToolbar
        selectedCount={3}
        onApproveAll={onApproveAll}
        onDenyAll={onDenyAll}
        onClear={onClear}
      />,
    );

    expect(screen.getByText("已选 3 条")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "批量 Approve" }));
    fireEvent.click(screen.getByRole("button", { name: "批量 Deny" }));
    fireEvent.click(screen.getByRole("button", { name: "清除" }));
    expect(onApproveAll).toHaveBeenCalledTimes(1);
    expect(onDenyAll).toHaveBeenCalledTimes(1);
    expect(onClear).toHaveBeenCalledTimes(1);
  });
});
