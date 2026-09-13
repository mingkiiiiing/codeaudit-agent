import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { createAudit, uploadAuditZip } from "../../api/client";
import NewAudit from "./index";

vi.mock("../../api/client", () => ({
  ApiError: class ApiError extends Error {
    constructor(
      public detail: string,
      public status: number,
    ) {
      super(detail);
    }
  },
  createAudit: vi.fn(),
  uploadAuditZip: vi.fn(),
}));

const mockCreateAudit = vi.mocked(createAudit);
const mockUploadAuditZip = vi.mocked(uploadAuditZip);

function renderNewAudit() {
  return render(
    <MemoryRouter initialEntries={["/new"]}>
      <Routes>
        <Route path="/new" element={<NewAudit />} />
        <Route path="/audits/:id" element={<div data-testid="detail-page">detail-page</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("NewAudit", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockCreateAudit.mockResolvedValue({ audit_id: "newtask1234" });
    mockUploadAuditZip.mockResolvedValue({ audit_id: "ziptask5678" });
  });

  it("路径为空时提交按钮 disabled", () => {
    renderNewAudit();
    expect(screen.getByRole("button", { name: "开始审计" })).toBeDisabled();
  });

  it("填入路径并提交后调用 createAudit 且携带表单值", async () => {
    const user = userEvent.setup();
    renderNewAudit();

    const input = screen.getByPlaceholderText("例如：D:\\demo\\my-project");
    await user.type(input, "D:\\demo\\demo-project");

    const submit = screen.getByRole("button", { name: "开始审计" });
    expect(submit).toBeEnabled();
    await user.click(submit);

    await waitFor(() =>
      expect(mockCreateAudit).toHaveBeenCalledWith({
        source_path: "D:\\demo\\demo-project",
        do_fix: false,
        do_tests: false,
      }),
    );
    // 创建成功后跳转详情页（当前为占位）
    expect(await screen.findByTestId("detail-page")).toBeInTheDocument();
  });

  it("切换到 zip 来源且未选文件时按钮保持 disabled", async () => {
    const user = userEvent.setup();
    renderNewAudit();

    await user.click(screen.getByText("上传 zip 包"));
    expect(screen.getByRole("button", { name: "开始审计" })).toBeDisabled();
    expect(mockUploadAuditZip).not.toHaveBeenCalled();
  });

  it("选择 zip 文件提交后调用 uploadAuditZip（FormData 由 client 层组装）", async () => {
    const user = userEvent.setup();
    renderNewAudit();

    await user.click(screen.getByText("上传 zip 包"));
    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement;
    const file = new File(["PK\x03\x04"], "my-project.zip", { type: "application/zip" });
    await user.upload(fileInput, file);

    expect(await screen.findByText(/已选择：my-project\.zip/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "开始审计" }));
    await waitFor(() =>
      expect(mockUploadAuditZip).toHaveBeenCalledWith(file, { do_fix: false, do_tests: false }),
    );
  });
});
