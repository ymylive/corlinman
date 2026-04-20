import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { ApprovalsEmptyState } from "../EmptyState";

describe("ApprovalsEmptyState", () => {
  it("renders pending copy on the pending tab", () => {
    render(<ApprovalsEmptyState tab="pending" />);
    expect(screen.getByText("当前无待审批工具调用")).toBeInTheDocument();
    expect(
      screen.getByText(/新请求会通过 SSE 自动推送/),
    ).toBeInTheDocument();
  });

  it("renders history copy on the history tab", () => {
    render(<ApprovalsEmptyState tab="history" />);
    expect(screen.getByText("历史记录为空")).toBeInTheDocument();
  });
});
