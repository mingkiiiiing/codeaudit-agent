import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { deleteAudit, getHealth, listAudits } from "../../api/client";
import Dashboard from "./index";

vi.mock("../../api/client", () => ({
  ApiError: class ApiError extends Error {
    constructor(
      public detail: string,
      public status: number,
    ) {
      super(detail);
    }
  },
  listAudits: vi.fn(),
  getHealth: vi.fn(),
  deleteAudit: vi.fn(),
}));

const mockListAudits = vi.mocked(listAudits);
const mockGetHealth = vi.mocked(getHealth);
const mockDeleteAudit = vi.mocked(deleteAudit);

const AUDITS = [
  {
    audit_id: "aaaaaaaa1111ffff",
    status: "done",
    created_at: "2026-09-13T10:00:00",
    source_path: "D:\\demo\\project-a",
    do_fix: false,
    do_tests: false,
    error: null,
  },
  {
    audit_id: "bbbbbbbb2222ffff",
    status: "running",
    created_at: "2026-09-13T10:05:00",
    source_path: "D:\\demo\\project-b",
    do_fix: true,
    do_tests: false,
    error: null,
  },
];

function renderDashboard() {
  return render(
    <MemoryRouter initialEntries={["/"]}>
      <Dashboard />
    </MemoryRouter>,
  );
}

describe("Dashboard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockListAudits.mockResolvedValue({ total: AUDITS.length, audits: AUDITS });
    mockGetHealth.mockResolvedValue({
      status: "ok",
      version: "0.5.0",
      audits: { active: 1, total: 2 },
    });
    mockDeleteAudit.mockResolvedValue(undefined);
  });

  it("渲染统计卡片与任务行（状态/路径/时间）", async () => {
    renderDashboard();

    expect(await screen.findByText("总任务")).toBeInTheDocument();
    const rowA = (await screen.findByText("aaaaaaaa")).closest("tr")!;
    expect(within(rowA).getByText("D:\\demo\\project-a")).toBeInTheDocument();
    expect(within(rowA).getByText("已完成")).toBeInTheDocument();
    expect(within(rowA).getByText("2026-09-13 10:00:00")).toBeInTheDocument();

    const rowB = screen.getByText("bbbbbbbb").closest("tr")!;
    expect(within(rowB).getByText("进行中")).toBeInTheDocument();
    expect(mockListAudits).toHaveBeenCalledWith(200, 0);
  });

  it("点击删除并确认后调用 deleteAudit 并重新拉取列表", async () => {
    renderDashboard();

    const row = (await screen.findByText("aaaaaaaa")).closest("tr")!;
    expect(row).not.toBeNull();

    fireEvent.click(within(row).getByRole("button", { name: "删除" }));
    fireEvent.click(await screen.findByRole("button", { name: "确认删除" }));

    await waitFor(() => expect(mockDeleteAudit).toHaveBeenCalledWith("aaaaaaaa1111ffff"));
    // 删除成功后重新拉取列表（初始 + 刷新 ≥ 2 次）
    await waitFor(() => expect(mockListAudits.mock.calls.length).toBeGreaterThanOrEqual(2));
    expect(await screen.findByText("任务已删除")).toBeInTheDocument();
  });

  it("空列表时展示 Empty 引导文案", async () => {
    mockListAudits.mockResolvedValue({ total: 0, audits: [] });
    renderDashboard();

    expect(await screen.findByText("暂无审计任务，点击右上角新建")).toBeInTheDocument();
  });
});
