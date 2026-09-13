/**
 * 七阶段流水线定义与阶段状态推导。
 * 语义规则全部沿用 web/index.html 演示页已验证逻辑：
 * error 优先 → 消息含「跳过/skipped」→ skipped；含「完成/就绪/已生成」→ done；否则 active；
 * 后续阶段出现时，前序仍 active 的视作完成；error/skipped 状态不被覆盖。
 */

import type { AuditEvent } from "../../api/types";

export interface StageDef {
  key: string;
  label: string;
}

/** 七阶段：接入 ingest → 索引 index → 理解 understand → 检测 detect → 修复 fix → 单测生成 testgen → 报告 report。 */
export const STAGE_DEFS: StageDef[] = [
  { key: "ingest", label: "接入" },
  { key: "index", label: "索引" },
  { key: "understand", label: "理解" },
  { key: "detect", label: "检测" },
  { key: "fix", label: "修复" },
  { key: "testgen", label: "单测生成" },
  { key: "report", label: "报告" },
];

const STAGE_KEYS: ReadonlySet<string> = new Set(STAGE_DEFS.map((s) => s.key));

export type StageStatus = "pending" | "active" | "done" | "skipped" | "error";

/** 阶段 key → 状态。 */
export type StageMap = Record<string, StageStatus>;

/** antd Steps 支持的状态。 */
export type StepStatus = "wait" | "process" | "finish" | "error";

/** 全 pending 的初始阶段表。 */
export function createStageMap(): StageMap {
  const map: StageMap = {};
  for (const def of STAGE_DEFS) map[def.key] = "pending";
  return map;
}

/** 单条 SSE 事件 → 该阶段的瞬时状态。 */
export function deriveStageStatus(event: AuditEvent): StageStatus {
  if (event.error) return "error";
  const message = event.message ?? "";
  if (/跳过|skipped/.test(message)) return "skipped";
  if (/完成|就绪|已生成/.test(message)) return "done";
  return "active";
}

/** 把事件应用到阶段表（返回新表）：前序 active 传播为 done；error/skipped 不被覆盖；未知阶段忽略。 */
export function applyStageEvent(prev: StageMap, event: AuditEvent): StageMap {
  const key = event.stage;
  if (!key || !STAGE_KEYS.has(key)) return prev;
  const idx = STAGE_DEFS.findIndex((def) => def.key === key);
  const next: StageMap = { ...prev };
  for (let i = 0; i < idx; i += 1) {
    const earlier = STAGE_DEFS[i]!.key;
    if (next[earlier] === "active") next[earlier] = "done";
  }
  if (!(next[key] === "error" || next[key] === "skipped")) {
    next[key] = deriveStageStatus(event);
  }
  return next;
}

/** 任务收尾：仍 active / 从未出现的阶段统一标记完成（error/skipped 保留，与演示页 finishStages 一致）。 */
export function finishStages(prev: StageMap): StageMap {
  const next: StageMap = { ...prev };
  for (const def of STAGE_DEFS) {
    if (next[def.key] === "active" || next[def.key] === "pending") next[def.key] = "done";
  }
  return next;
}

/** StageStatus → antd Steps status（skipped 显示为灰色 finish，配合「已跳过」文案）。 */
export function stageStepStatus(status: StageStatus): StepStatus {
  switch (status) {
    case "pending":
      return "wait";
    case "active":
      return "process";
    case "done":
    case "skipped":
      return "finish";
    case "error":
      return "error";
  }
}
