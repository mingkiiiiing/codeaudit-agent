/**
 * SevTag 组件测试（W15-F 卡F）：四级严重度的中文文案与品牌语义色。
 * antd Tag 语义：预设色走 ant-tag-* 类；自定义 hex 色走内联 backgroundColor + ant-tag-has-color 类。
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { SevTag } from "./SevTag";

// [severity, 中文文案, 品牌色 hex, rgb 三元组]（hex 与 src/utils/format.ts sevColor 对齐；
// jsdom 会把内联色值规范化为 rgb(...)，故断言用 rgb 形式）
const SEV_CASES: Array<[string, string, string, string]> = [
  ["critical", "致命", "#b91c1c", "rgb(185, 28, 28)"],
  ["high", "严重", "#ea580c", "rgb(234, 88, 12)"],
  ["medium", "中等", "#ca8a04", "rgb(202, 138, 4)"],
  ["low", "轻微", "#2563eb", "rgb(37, 99, 235)"],
];

describe("SevTag", () => {
  it.each(SEV_CASES)("severity=%s 渲染文案「%s」与品牌色 %s", (severity, label, _hex, rgb) => {
    render(<SevTag severity={severity} />);

    const tag = screen.getByText(label);
    expect(tag).toBeInTheDocument();
    expect(tag.className).toContain("ant-tag");
    // 自定义色以内联 backgroundColor 落到 DOM（jsdom 规范化为 rgb 形式）
    expect(tag.getAttribute("style")).toContain(`background-color: ${rgb}`);
  });

  it("未知 severity 回退：文案原样展示、颜色回退灰", () => {
    render(<SevTag severity="unknown" />);

    const tag = screen.getByText("unknown");
    expect(tag.className).toContain("ant-tag");
    expect(tag.getAttribute("style")).toContain("background-color: rgb(100, 116, 139)");
  });
});
