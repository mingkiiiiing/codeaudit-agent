/**
 * RenameTool 组件测试（W30 卡D）：
 * 空表单禁用 / dry-run 预览渲染 diff / 确认应用成功流 / 400 detail / 409 含「未写入」提示 /
 * 401 detail / 非 ApiError 网络失败兜底。mock 方式沿 ReportHistory.test.tsx（vi.mock 整个 api/client）。
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, renameSymbol } from "../api/client";
import type { RenameResult } from "../api/client";
import { RenameTool } from "./RenameTool";

vi.mock("../api/client", () => ({
  ApiError: class ApiError extends Error {
    constructor(
      public detail: string,
      public status: number,
    ) {
      super(detail);
    }
  },
  renameSymbol: vi.fn(),
}));

const mockRenameSymbol = vi.mocked(renameSymbol);

const DRY_RUN: RenameResult = {
  ok: true,
  applied: false,
  files: ["demo/util.py", "demo/main.py"],
  replace_points: 2,
  diffs: {
    "demo/util.py":
      "--- a/demo/util.py\n+++ b/demo/util.py\n@@ -1,2 +1,2 @@\n-def old_name():\n+def new_name():",
    "demo/main.py":
      "--- a/demo/main.py\n+++ b/demo/main.py\n@@ -1,2 +1,2 @@\n-old_name()\n+new_name()",
  },
  errors: [],
};

const APPLIED: RenameResult = {
  ok: true,
  applied: true,
  files: ["demo/util.py", "demo/main.py"],
  replace_points: 2,
  diffs: DRY_RUN.diffs,
  errors: [],
};

function fillForm(): void {
  fireEvent.change(screen.getByPlaceholderText(/服务端白名单内的/), {
    target: { value: "D:\\demo\\my-project" },
  });
  fireEvent.change(screen.getByPlaceholderText(/^现名/), {
    target: { value: "old_name" },
  });
  fireEvent.change(screen.getByPlaceholderText(/^新名/), {
    target: { value: "new_name" },
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  mockRenameSymbol.mockResolvedValue(DRY_RUN);
});

describe("RenameTool", () => {
  it("空表单：生成预览禁用、确认应用（未预览）禁用；填齐后仅预览可用", () => {
    render(<RenameTool />);

    const previewBtn = screen.getByRole("button", { name: "生成预览" });
    const applyBtn = screen.getByRole("button", { name: "确认应用（写入源码）" });
    expect(previewBtn).toBeDisabled();
    expect(applyBtn).toBeDisabled();
    expect(mockRenameSymbol).not.toHaveBeenCalled();

    fillForm();
    expect(previewBtn).not.toBeDisabled();
    // 预览仍未生成：应用按钮保持禁用（先预览才能应用）
    expect(applyBtn).toBeDisabled();
  });

  it("生成预览：请求带 apply=false，渲染替换点/文件数与逐文件 unified diff", async () => {
    render(<RenameTool />);
    fillForm();
    fireEvent.click(screen.getByRole("button", { name: "生成预览" }));

    await waitFor(() =>
      expect(mockRenameSymbol).toHaveBeenCalledWith({
        source_path: "D:\\demo\\my-project",
        old_name: "old_name",
        new_name: "new_name",
        apply: false,
      }),
    );
    expect(await screen.findByTestId("rename-preview")).toBeInTheDocument();
    expect(screen.getByText("替换点 2 处")).toBeInTheDocument();
    expect(screen.getByText("影响文件 2 个")).toBeInTheDocument();
    // 逐文件 diff：文件名 + diff 正文（等宽 pre）
    expect(screen.getByText("demo/util.py")).toBeInTheDocument();
    expect(screen.getByText("demo/main.py")).toBeInTheDocument();
    expect(screen.getByText(/def old_name\(\)/)).toBeInTheDocument();
    expect(screen.getByText(/\+def new_name\(\)/)).toBeInTheDocument();
  });

  it("确认应用成功：请求带 apply=true，展示已写入文件数与 applied 状态", async () => {
    mockRenameSymbol.mockResolvedValueOnce(DRY_RUN).mockResolvedValueOnce(APPLIED);
    render(<RenameTool />);
    fillForm();
    fireEvent.click(screen.getByRole("button", { name: "生成预览" }));
    const applyBtn = screen.getByRole("button", { name: "确认应用（写入源码）" });
    await waitFor(() => expect(applyBtn).not.toBeDisabled());

    fireEvent.click(applyBtn);

    await waitFor(() =>
      expect(mockRenameSymbol).toHaveBeenCalledWith({
        source_path: "D:\\demo\\my-project",
        old_name: "old_name",
        new_name: "new_name",
        apply: true,
      }),
    );
    expect(await screen.findByText("已写入 2 文件")).toBeInTheDocument();
    expect(screen.getByText(/applied=true/)).toBeInTheDocument();
  });

  it("预览 400（plan 拒绝）：展示服务端中文 detail，应用按钮保持禁用", async () => {
    mockRenameSymbol.mockRejectedValue(
      new ApiError("重命名计划阶段即拒绝（未做任何修改）：old==new", 400),
    );
    render(<RenameTool />);
    fillForm();
    fireEvent.click(screen.getByRole("button", { name: "生成预览" }));

    expect(
      await screen.findByText("重命名计划阶段即拒绝（未做任何修改）：old==new"),
    ).toBeInTheDocument();
    // 预览未生成：应用按钮仍禁用，页面未崩
    expect(screen.getByRole("button", { name: "确认应用（写入源码）" })).toBeDisabled();
    expect(screen.queryByTestId("rename-preview")).not.toBeInTheDocument();
  });

  it("应用 409（复检失败）：展示 detail 并额外提示 all-or-nothing 未写入任何文件", async () => {
    mockRenameSymbol
      .mockResolvedValueOnce(DRY_RUN)
      .mockRejectedValueOnce(
        new ApiError("重命名应用失败（all-or-nothing，未写入任何文件）：demo/util.py 内容已变化", 409),
      );
    render(<RenameTool />);
    fillForm();
    fireEvent.click(screen.getByRole("button", { name: "生成预览" }));
    const applyBtn = screen.getByRole("button", { name: "确认应用（写入源码）" });
    await waitFor(() => expect(applyBtn).not.toBeDisabled());

    fireEvent.click(applyBtn);

    expect(
      await screen.findByText(
        "重命名应用失败（all-or-nothing，未写入任何文件）：demo/util.py 内容已变化",
      ),
    ).toBeInTheDocument();
    // 409 额外提示
    expect(
      await screen.findByText(/all-or-nothing：未写入任何文件，源码保持原样/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/已写入 .* 文件/)).not.toBeInTheDocument();
  });

  it("预览 401（未授权）：展示中文 detail，不崩页面", async () => {
    mockRenameSymbol.mockRejectedValue(new ApiError("未授权：token 缺失或无效", 401));
    render(<RenameTool />);
    fillForm();
    fireEvent.click(screen.getByRole("button", { name: "生成预览" }));

    expect(await screen.findByText("未授权：token 缺失或无效")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "生成预览" })).not.toBeDisabled();
  });

  it("renameSymbol 抛普通 Error（网络失败）：展示兜底文案，不崩页面", async () => {
    mockRenameSymbol.mockRejectedValue(new Error("boom"));
    render(<RenameTool />);
    fillForm();
    fireEvent.click(screen.getByRole("button", { name: "生成预览" }));

    expect(await screen.findByText("重命名请求失败：Error: boom")).toBeInTheDocument();
    expect(screen.queryByTestId("rename-preview")).not.toBeInTheDocument();
  });
});
