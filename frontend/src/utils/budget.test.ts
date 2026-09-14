/**
 * utils/budget.ts 单元测试：SSE 事件 / report 的熔断信号提取、合并、文案与会话缓存。
 * 事件形状与 audit/orchestrator/pipeline.py 实际 emit 的字段一致。
 */

import { describe, expect, it } from "vitest";

import type { AuditEvent } from "../api/types";
import {
  extractBudgetFromEvent,
  extractBudgetFromReport,
  formatBudgetMessage,
  getDegradedAudit,
  mergeBudgetInfo,
  rememberDegradedAudit,
  resetDegradedCache,
} from "./budget";

describe("extractBudgetFromEvent", () => {
  it("熔断即时告警帧（stage:init，warning + 数字）→ 提取消耗与预算", () => {
    const ev: AuditEvent = {
      type: "progress",
      stage: "init",
      message: "Token 预算已耗尽：已消耗 20000 tokens（预算 20000）",
      warning: true,
      used_tokens: 20000,
      token_budget: 20000,
    };
    expect(extractBudgetFromEvent(ev)).toEqual({ usedTokens: 20000, tokenBudget: 20000 });
  });

  it("done 进度帧（budget_tripped + 数字）→ 提取消耗与预算", () => {
    const ev: AuditEvent = {
      type: "progress",
      stage: "done",
      message: "审计完成：…",
      degraded: true,
      budget_tripped: true,
      used_tokens: 23456,
      token_budget: 20000,
    };
    expect(extractBudgetFromEvent(ev)).toEqual({ usedTokens: 23456, tokenBudget: 20000 });
  });

  it("fix/testgen 跳过帧（仅 budget_tripped，无数字）→ 信号有效但数字为 null", () => {
    const ev: AuditEvent = {
      type: "progress",
      stage: "fix",
      message: "跳过：Token 预算已耗尽，不生成修复补丁",
      budget_tripped: true,
    };
    expect(extractBudgetFromEvent(ev)).toEqual({ usedTokens: null, tokenBudget: null });
  });

  it("普通进度帧 / 服务端合成终帧 → 无信号返回 null", () => {
    expect(extractBudgetFromEvent({ type: "progress", stage: "detect", message: "检测中" })).toBeNull();
    expect(extractBudgetFromEvent({ type: "done" })).toBeNull();
    expect(extractBudgetFromEvent(null)).toBeNull();
    expect(extractBudgetFromEvent(undefined)).toBeNull();
  });
});

describe("extractBudgetFromReport", () => {
  it("当前后端契约（report/stats 无预算字段）→ 恒返回 null（诚实降级）", () => {
    const report = {
      audit_id: "x",
      stats: { llm_calls: 4, prompt_tokens: 1200, completion_tokens: 340 },
    };
    expect(extractBudgetFromReport(report)).toBeNull();
    expect(extractBudgetFromReport(null)).toBeNull();
    expect(extractBudgetFromReport(undefined)).toBeNull();
  });

  it("前向兼容：report 携带 budget_tripped/used_tokens/token_budget 时可提取（根或 stats）", () => {
    expect(
      extractBudgetFromReport({ budget_tripped: true, used_tokens: 100, token_budget: 50 }),
    ).toEqual({ usedTokens: 100, tokenBudget: 50 });
    expect(
      extractBudgetFromReport({
        budget_tripped: true,
        stats: { used_tokens: 80, token_budget: 50 },
      }),
    ).toEqual({ usedTokens: 80, tokenBudget: 50 });
    // 标志存在但数字缺失 → null 占位
    expect(extractBudgetFromReport({ budget_tripped: true })).toEqual({
      usedTokens: null,
      tokenBudget: null,
    });
  });
});

describe("mergeBudgetInfo / formatBudgetMessage", () => {
  it("合并：保留已确认信号，数字用后到者补齐", () => {
    expect(mergeBudgetInfo(null, { usedTokens: 1, tokenBudget: 2 })).toEqual({
      usedTokens: 1,
      tokenBudget: 2,
    });
    expect(mergeBudgetInfo({ usedTokens: 1, tokenBudget: 2 }, null)).toEqual({
      usedTokens: 1,
      tokenBudget: 2,
    });
    expect(
      mergeBudgetInfo({ usedTokens: null, tokenBudget: null }, { usedTokens: 10, tokenBudget: 20 }),
    ).toEqual({ usedTokens: 10, tokenBudget: 20 });
    expect(mergeBudgetInfo(null, null)).toBeNull();
  });

  it("文案：数字齐全含消耗/预算从句；缺失时诚实省略", () => {
    expect(formatBudgetMessage({ usedTokens: 12345, tokenBudget: 20000 })).toBe(
      "本次审计触发 Token 预算熔断，后续 LLM 阶段已降级：已消耗 12345 / 预算 20000 tokens",
    );
    expect(formatBudgetMessage({ usedTokens: null, tokenBudget: 20000 })).toBe(
      "本次审计触发 Token 预算熔断，后续 LLM 阶段已降级。",
    );
  });
});

describe("会话级降级缓存", () => {
  it("登记后可查询；未登记返回 null；空 ID 忽略；reset 清空", () => {
    resetDegradedCache();
    expect(getDegradedAudit("task-1")).toBeNull();

    rememberDegradedAudit("task-1", { usedTokens: 100, tokenBudget: 50 });
    expect(getDegradedAudit("task-1")).toEqual({ usedTokens: 100, tokenBudget: 50 });

    rememberDegradedAudit("", { usedTokens: 1, tokenBudget: 1 });
    expect(getDegradedAudit("")).toBeNull();

    resetDegradedCache();
    expect(getDegradedAudit("task-1")).toBeNull();
  });
});
