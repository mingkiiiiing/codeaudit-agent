/**
 * ReportHistory 组件测试（W29 卡D）：
 * 多版本列表渲染 / 空态文案 / 版本点击展开看该 seq 关键摘要 / 列表接口失败（含 404）降级空态 /
 * 版本详情加载失败行内降级。mock 方式沿 TaskDetail.test.tsx（vi.mock 整个 api/client）。
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, getReport, listReports } from "../api/client";
import type { ReportVersionDetail } from "../api/client";
import { ReportHistory } from "./ReportHistory";

vi.mock("../api/client", () => ({
  ApiError: class ApiError extends Error {
    constructor(
      public detail: string,
      public status: number,
    ) {
      super(detail);
    }
  },
  listReports: vi.fn(),
  getReport: vi.fn(),
}));

const mockListReports = vi.mocked(listReports);
const mockGetReport = vi.mocked(getReport);

const VERSIONS = {
  total: 3,
  reports: [
    { seq: 1, created_at: "2026-09-17T09:00:00", health_score: 55, issue_count: 12 },
    { seq: 2, created_at: "2026-09-18T10:30:00", health_score: 76.5, issue_count: 8 },
    { seq: 3, created_at: "2026-09-19T08:15:00", health_score: 88, issue_count: 4 },
  ],
};

function detailFor(health_score: number): ReportVersionDetail {
  return {
    audit_id: "aaaaaaaa1111ffff",
    project_name: "demo_proj",
    languages: { python: 1 },
    loc: 1200,
    health_score,
    summary: { critical: 1, high: 2, medium: 3, low: 4 },
    created_at: "2026-09-18T10:30:00",
    schema_version: "1.7",
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  mockListReports.mockResolvedValue(VERSIONS);
  mockGetReport.mockResolvedValue(detailFor(76.5));
});

describe("ReportHistory", () => {
  it("多版本列表：渲染版本号/时间/健康分/问题数与总数行", async () => {
    render(<ReportHistory auditId="aaaaaaaa1111ffff" />);

    expect(
      await screen.findByText("共 3 个历史版本（当前报告即最新版本）"),
    ).toBeInTheDocument();
    expect(screen.getByText("版本 1")).toBeInTheDocument();
    expect(screen.getByText("版本 2")).toBeInTheDocument();
    expect(screen.getByText("版本 3")).toBeInTheDocument();
    // 时间按 fmtDateTime 格式化为 "YYYY-MM-DD HH:mm:ss"
    expect(screen.getByText("2026-09-18 10:30:00")).toBeInTheDocument();
    expect(screen.getByText(/健康分 55/)).toBeInTheDocument();
    expect(screen.getByText(/健康分 76\.5/)).toBeInTheDocument();
    expect(screen.getByText(/健康分 88/)).toBeInTheDocument();
    expect(screen.getByText("问题 12")).toBeInTheDocument();
    expect(screen.getByText("问题 4")).toBeInTheDocument();
    // 未展开时不请求单版本端点
    expect(mockGetReport).not.toHaveBeenCalled();
  });

  it("空列表：显示「暂无历史版本」空态", async () => {
    mockListReports.mockResolvedValue({ total: 0, reports: [] });
    render(<ReportHistory auditId="aaaaaaaa1111ffff" />);

    expect(await screen.findByText("暂无历史版本")).toBeInTheDocument();
    expect(screen.queryByText(/^共 .* 个历史版本/)).not.toBeInTheDocument();
  });

  it("点击版本展开：拉取该 seq 版本端点并显示健康分/严重度计数等关键摘要", async () => {
    render(<ReportHistory auditId="aaaaaaaa1111ffff" />);
    expect(await screen.findByText("版本 2")).toBeInTheDocument();

    fireEvent.click(screen.getByText("版本 2"));

    await waitFor(() =>
      expect(mockGetReport).toHaveBeenCalledWith("aaaaaaaa1111ffff", 2),
    );
    // 版本端点关键字段：健康分 / 四级严重度计数 / 代码行数 / 生成时间
    // （getByText 只匹配直接文本节点：分值嵌套在强色 span 内，分开断言）
    expect(await screen.findByText("健康分 / 100")).toBeInTheDocument();
    expect(screen.getByText("76.5")).toBeInTheDocument();
    expect(screen.getByText("致命")).toBeInTheDocument();
    expect(screen.getByText("严重")).toBeInTheDocument();
    expect(screen.getByText("中等")).toBeInTheDocument();
    expect(screen.getByText("轻微")).toBeInTheDocument();
    expect(screen.getByText(/代码行数 1200/)).toBeInTheDocument();
    expect(screen.getByText(/生成于 2026-09-18 10:30:00/)).toBeInTheDocument();
  });

  it("列表接口失败（网络/500）：降级为「暂无历史版本」，不弹错误阻断页面", async () => {
    mockListReports.mockRejectedValue(new Error("网络请求失败：boom"));
    render(<ReportHistory auditId="aaaaaaaa1111ffff" />);

    expect(await screen.findByText("暂无历史版本")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("列表 404（任务不存在）：同样降级为「暂无历史版本」", async () => {
    mockListReports.mockRejectedValue(new ApiError("任务不存在：aaaaaaaa1111ffff", 404));
    render(<ReportHistory auditId="aaaaaaaa1111ffff" />);

    expect(await screen.findByText("暂无历史版本")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("版本详情加载失败：行内降级文案，不影响列表本身", async () => {
    mockGetReport.mockRejectedValue(new ApiError("报告版本不存在：seq=1", 404));
    render(<ReportHistory auditId="aaaaaaaa1111ffff" />);
    expect(await screen.findByText("版本 1")).toBeInTheDocument();

    fireEvent.click(screen.getByText("版本 1"));

    expect(
      await screen.findByText("该版本摘要加载失败，可收起后重新展开重试。"),
    ).toBeInTheDocument();
    // 列表行仍在
    expect(screen.getByText("版本 3")).toBeInTheDocument();
  });
});
