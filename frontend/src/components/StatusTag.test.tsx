/**
 * StatusTag 组件测试（W15-F 卡F）：
 * done/failed 用绿/红 Tag，queued/running 用 processing Badge，未知状态回退默认 Tag + 原文。
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { StatusTag } from "./StatusTag";

describe("StatusTag", () => {
  it("done：绿色 Tag，文案「已完成」，不出现 Badge", () => {
    render(<StatusTag status="done" />);

    const tag = screen.getByText("已完成");
    expect(tag.className).toContain("ant-tag-green");
    expect(tag.className).not.toContain("ant-tag-red");
    expect(document.querySelector(".ant-badge")).toBeNull();
  });

  it("failed：红色 Tag，文案「失败」", () => {
    render(<StatusTag status="failed" />);

    const tag = screen.getByText("失败");
    expect(tag.className).toContain("ant-tag-red");
    expect(tag.className).not.toContain("ant-tag-green");
  });

  it("queued：processing Badge + 文案「排队中」", () => {
    render(<StatusTag status="queued" />);

    expect(screen.getByText("排队中").className).toContain("ant-badge-status-text");
    expect(
      document.querySelector(".ant-badge-status-dot.ant-badge-status-processing"),
    ).not.toBeNull();
  });

  it("running：processing Badge + 文案「进行中」", () => {
    render(<StatusTag status="running" />);

    expect(screen.getByText("进行中").className).toContain("ant-badge-status-text");
    expect(
      document.querySelector(".ant-badge-status-dot.ant-badge-status-processing"),
    ).not.toBeNull();
  });

  it("未知状态：默认 Tag 原样展示、不带绿/红色、不出现 Badge", () => {
    render(<StatusTag status="paused" />);

    const tag = screen.getByText("paused");
    expect(tag.className).toContain("ant-tag");
    expect(tag.className).not.toContain("ant-tag-green");
    expect(tag.className).not.toContain("ant-tag-red");
    expect(document.querySelector(".ant-badge")).toBeNull();
  });
});
