import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, getAudit, getSummary, getUnderstand, listIssues, listPatches, listRefactors, subscribeEvents } from "../../api/client";
import type { AuditEvent } from "../../api/types";
import TaskDetail from "./index";

vi.mock("../../api/client", () => ({
  ApiError: class ApiError extends Error {
    constructor(
      public detail: string,
      public status: number,
    ) {
      super(detail);
    }
  },
  getAudit: vi.fn(),
  getSummary: vi.fn(),
  listIssues: vi.fn(),
  listPatches: vi.fn(),
  listRefactors: vi.fn(),
  getUnderstand: vi.fn(),
  deleteAudit: vi.fn(),
  reportUrl: vi.fn(
    (id: string, fmt: string) => `/api/audits/${id}/report?format=${fmt}`,
  ),
  subscribeEvents: vi.fn(),
}));

const mockGetAudit = vi.mocked(getAudit);
const mockGetSummary = vi.mocked(getSummary);
const mockSubscribe = vi.mocked(subscribeEvents);

vi.mocked(listIssues).mockResolvedValue({
  total: 1,
  issues: [
    {
      id: "PY-001",
      severity: "critical",
      category: "security",
      file: "app/db.py",
      line_start: 42,
      line_end: 42,
      title: "SQL 拼接注入",
      description: "字符串拼接构造 SQL",
      suggestion: "参数化查询",
      evidence: ["execute(f\"...{name}\")"],
      code_snippet: "cursor.execute(f\"SELECT * FROM u WHERE n='{name}'\")",
      confidence: 0.95,
      source: "rule",
      fix_status: "verified",
      patch_id: "patch-1",
    },
  ],
});
vi.mocked(listPatches).mockResolvedValue({
  total: 1,
  patches: [
    {
      id: "patch-1",
      issue_id: "PY-001",
      diff: "--- a/app/db.py\n+++ b/app/db.py\n@@ -1,1 +1,1 @@\n-old = 1\n+new = 1",
      apply_status: "verified",
      rationale: "参数化",
      tests_run: 2,
      tests_passed: 2,
    },
  ],
});
vi.mocked(listRefactors).mockResolvedValue({
  total: 1,
  proposals: [
    {
      id: "rp-1",
      title: "拆分过大的服务模块",
      target: "app/services/orders.py",
      kind: "split-module",
      rationale: "热点模块",
      steps: ["识别边界", "拆出 billing"],
      benefits: "职责清晰",
      related_issues: ["PY-001"],
      source: "heuristic",
      confidence: 0.8,
    },
  ],
});
vi.mocked(getUnderstand).mockResolvedValue({
  architecture: null,
});

const SUMMARY = {
  audit_id: "aaaaaaaa1111ffff",
  project_name: "demo_proj",
  health_score: 88,
  summary: { critical: 1, high: 0, medium: 2, low: 3 },
  total_issues: 6,
  loc: 1200,
  duration_sec: 3.21,
  tokens: {
    llm_calls: 4,
    prompt_tokens: 1200,
    completion_tokens: 340,
    cache_hits: 2,
    cache_misses: 2,
  },
};

const DONE = { audit_id: "aaaaaaaa1111ffff", status: "done", error: null };
const FAILED = { audit_id: "aaaaaaaa1111ffff", status: "failed", error: "ValueError: boom" };
const RUNNING = { audit_id: "aaaaaaaa1111ffff", status: "running", error: null };

function renderDetail(id = "aaaaaaaa1111ffff") {
  return render(
    <MemoryRouter initialEntries={[`/audits/${id}`]}>
      <Routes>
        <Route path="/audits/:id" element={<TaskDetail />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("TaskDetail", () => {
  let sse: {
    onEvent: (e: AuditEvent) => void;
    onDone: () => void;
    onError: (e: unknown) => void;
  } | null;

  beforeEach(() => {
    vi.clearAllMocks();
    sse = null;
    mockSubscribe.mockImplementation((_id, onEvent, onDone, onError) => {
      sse = { onEvent, onDone, onError };
      return () => {
        sse = null;
      };
    });
    mockGetSummary.mockResolvedValue(SUMMARY);
  });

  it("done 任务：概览 + 四个 Tab 与问题/补丁/重构方案内容", async () => {
    mockGetAudit.mockResolvedValue(DONE);
    renderDetail();

    expect(await screen.findByText(/健康分/, {}, { timeout: 8000 })).toBeInTheDocument();
    expect(screen.getByText("demo_proj")).toBeInTheDocument();
    expect(await screen.findByText("SQL 拼接注入", {}, { timeout: 8000 })).toBeInTheDocument(); // 默认问题 Tab

    fireEvent.click(screen.getByRole("tab", { name: "修复补丁" }));
    expect(await screen.findByText("patch-1")).toBeInTheDocument();
    expect(screen.getByText("+new = 1")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "重构方案" }));
    expect(await screen.findByText("拆分过大的服务模块")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "架构理解" }));
    expect(
      await screen.findByText(/该任务未生成架构卡片/, {}, { timeout: 8000 }),
    ).toBeInTheDocument();

    expect(screen.getByText("report.html")).toHaveAttribute(
      "href",
      "/api/audits/aaaaaaaa1111ffff/report?format=html",
    );
  }, 30000);

  it("failed 任务：进度面板显示失败 Alert 与原因", async () => {
    mockGetAudit.mockResolvedValue(FAILED);
    renderDetail();

    expect(await screen.findByText("任务失败")).toBeInTheDocument();
    expect(screen.getByText(/ValueError: boom/)).toBeInTheDocument();
    expect(mockSubscribe).not.toHaveBeenCalled(); // 终态不订阅
  });

  it("running 任务：订阅 SSE，阶段事件驱动进度，done 终帧后加载结果", async () => {
    mockGetAudit
      .mockResolvedValueOnce(RUNNING)
      .mockResolvedValueOnce(DONE);
    renderDetail();

    await waitFor(() => expect(mockSubscribe).toHaveBeenCalled());
    sse!.onEvent({ stage: "detect", message: "检测中 3/10" });
    expect((await screen.findAllByText("[detect]")).length).toBeGreaterThanOrEqual(1);
    expect((await screen.findAllByText(/检测中 3\/10/)).length).toBeGreaterThanOrEqual(1);

    sse!.onEvent({ stage: "detect", message: "检测完成" });
    sse!.onDone();
    expect(await screen.findByText(/健康分/)).toBeInTheDocument();
    expect(mockGetSummary).toHaveBeenCalledWith("aaaaaaaa1111ffff");
  });

  it("预算熔断：SSE 熔断事件即显警示条（含消耗/预算），done 后仍在", async () => {
    mockGetAudit
      .mockResolvedValueOnce(RUNNING)
      .mockResolvedValueOnce(DONE);
    renderDetail();

    await waitFor(() => expect(mockSubscribe).toHaveBeenCalled());
    // 熔断即时告警帧（audit/orchestrator/pipeline.py _BudgetGateLLM._trip 的形状）
    sse!.onEvent({
      stage: "init",
      message: "Token 预算已耗尽：已消耗 12345 tokens（预算 20000）",
      warning: true,
      used_tokens: 12345,
      token_budget: 20000,
    });
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /已消耗 12345 \/ 预算 20000 tokens/,
    );

    sse!.onDone();
    expect(await screen.findByText(/健康分/)).toBeInTheDocument();
    // done 视图：警示条仍位于审计概览（健康分卡片）上方
    expect(
      screen.getByText("本次审计触发 Token 预算熔断，后续 LLM 阶段已降级：已消耗 12345 / 预算 20000 tokens"),
    ).toBeInTheDocument();
  });

  it("done 任务无熔断数据：不渲染预算警示条（诚实降级）", async () => {
    mockGetAudit.mockResolvedValue(DONE);
    renderDetail();

    expect(await screen.findByText(/健康分/)).toBeInTheDocument();
    expect(screen.queryByText(/Token 预算熔断/)).not.toBeInTheDocument();
  });

  it("404：内存态任务表提示", async () => {
    mockGetAudit.mockRejectedValue(new ApiError("任务不存在：x", 404));
    renderDetail();

    expect(await screen.findByText("任务不存在")).toBeInTheDocument();
    expect(screen.getByText(/内存态/)).toBeInTheDocument();
  });
});
