import { describe, expect, it } from "vitest";

import {
  CAT_LABEL,
  fmtDateTime,
  fmtDuration,
  SEV_LABEL,
  sevColor,
  shortId,
  STATUS_LABEL,
} from "./format";

describe("format utils", () => {
  it("sevColor 使用演示页品牌语义色", () => {
    expect(sevColor("critical")).toBe("#b91c1c");
    expect(sevColor("high")).toBe("#ea580c");
    expect(sevColor("medium")).toBe("#ca8a04");
    expect(sevColor("low")).toBe("#2563eb");
  });

  it("标签映射与演示页一致", () => {
    expect(SEV_LABEL).toEqual({ critical: "致命", high: "严重", medium: "中等", low: "轻微" });
    expect(CAT_LABEL).toEqual({ bug: "缺陷", performance: "性能", style: "规范", security: "安全" });
    expect(STATUS_LABEL).toEqual({
      queued: "排队中",
      running: "进行中",
      done: "已完成",
      failed: "失败",
    });
  });

  it("shortId 取前 8 位", () => {
    expect(shortId("abcdefgh1234")).toBe("abcdefgh");
    expect(shortId("short")).toBe("short");
    expect(shortId("")).toBe("");
  });

  it("fmtDuration：<60s 显示秒，否则分秒", () => {
    expect(fmtDuration(3.14159)).toBe("3.14 s");
    expect(fmtDuration(59.99)).toBe("59.99 s");
    expect(fmtDuration(125)).toBe("2 分 5 秒");
    expect(fmtDuration(0)).toBe("0.00 s");
  });

  it("fmtDateTime：ISO → YYYY-MM-DD HH:mm:ss，空值返回 -", () => {
    expect(fmtDateTime("2026-09-13T10:20:30")).toBe("2026-09-13 10:20:30");
    expect(fmtDateTime(null)).toBe("-");
    expect(fmtDateTime(undefined)).toBe("-");
    expect(fmtDateTime("not-a-date")).toBe("-");
  });
});
