/**
 * BudgetAlert 组件测试（W13-A4）：数据有 / 无两态。
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { BudgetAlert } from "./BudgetAlert";

describe("BudgetAlert", () => {
  it("有熔断数据：渲染 warning 警示条，文案含消耗与预算 tokens", () => {
    render(<BudgetAlert info={{ usedTokens: 12345, tokenBudget: 20000 }} />);

    const alert = screen.getByRole("alert");
    expect(alert).toBeInTheDocument();
    expect(alert.className).toContain("ant-alert-warning");
    expect(
      screen.getByText(
        "本次审计触发 Token 预算熔断，后续 LLM 阶段已降级：已消耗 12345 / 预算 20000 tokens",
      ),
    ).toBeInTheDocument();
  });

  it("只有熔断标志、无数字：渲染警示条但不伪造数字", () => {
    render(<BudgetAlert info={{ usedTokens: null, tokenBudget: null }} />);

    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(
      screen.getByText("本次审计触发 Token 预算熔断，后续 LLM 阶段已降级。"),
    ).toBeInTheDocument();
  });

  it("无数据（null，旧任务/未熔断）：不渲染任何警示条", () => {
    const { container } = render(<BudgetAlert info={null} />);

    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
