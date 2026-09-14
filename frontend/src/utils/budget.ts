/**
 * Token 预算熔断降级信息的前端推导工具（W13-A4）。
 *
 * 真实数据契约（已核对 audit/orchestrator/pipeline.py 与 server/app.py）：
 * - SSE 进度事件（type="progress"）中三类帧携带熔断信号：
 *   1) 熔断即时告警：{stage:"init", warning:true, used_tokens, token_budget}；
 *   2) fix/testgen 跳过：{stage:"fix"|"testgen", budget_tripped:true}（无数字）；
 *   3) 终帧前的 done 进度事件：{stage:"done", degraded:true, budget_tripped:true,
 *      used_tokens, token_budget}（used_tokens 为闸门最终累计口径）。
 * - 服务端合成的终帧 {type:"done"}、GET /api/audits/{id}（含 report.stats）、
 *   GET /summary、任务列表接口均不含预算字段——旧任务 / 页面刷新后拿不到数据，
 *   诚实降级：不显示警示 / 标记，不伪造。
 */

import type { AuditEvent } from "../api/types";

/** 一次预算熔断的已知信息；数字缺失时为 null（事件帧可能只带标志不带数字）。 */
export interface BudgetTripInfo {
  usedTokens: number | null;
  tokenBudget: number | null;
}

function asFiniteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/**
 * 从单条 SSE 事件提取熔断信号：
 * budget_tripped === true，或同时携带 used_tokens/token_budget 数字（熔断即时告警帧）
 * 视为有效信号；普通进度帧 / 合成终帧返回 null。
 */
export function extractBudgetFromEvent(event: AuditEvent | null | undefined): BudgetTripInfo | null {
  if (!event) return null;
  const tripped = event.budget_tripped === true;
  const used = asFiniteNumber(event.used_tokens);
  const budget = asFiniteNumber(event.token_budget);
  if (!tripped && (used === null || budget === null)) return null;
  return { usedTokens: used, tokenBudget: budget };
}

/**
 * 从 GET /api/audits/{id} 的 report 原始结构推导（防御性前向兼容）：
 * 当前 后端 AuditStats 不含 budget_tripped 等字段，本函数实际恒返回 null；
 * 若后端未来在 report / report.stats 上补齐同名字段，前端无需改动即可生效。
 */
export function extractBudgetFromReport(
  report: Record<string, unknown> | null | undefined,
): BudgetTripInfo | null {
  if (!report || report.budget_tripped !== true) return null;
  const stats = (report.stats ?? {}) as Record<string, unknown>;
  return {
    usedTokens: asFiniteNumber(report.used_tokens) ?? asFiniteNumber(stats.used_tokens),
    tokenBudget: asFiniteNumber(report.token_budget) ?? asFiniteNumber(stats.token_budget),
  };
}

/** 合并两条熔断信息：已确认熔断即保持；数字字段用后到者补齐（done 帧口径最准）。 */
export function mergeBudgetInfo(
  prev: BudgetTripInfo | null,
  next: BudgetTripInfo | null,
): BudgetTripInfo | null {
  if (!prev) return next;
  if (!next) return prev;
  return {
    usedTokens: next.usedTokens ?? prev.usedTokens,
    tokenBudget: next.tokenBudget ?? prev.tokenBudget,
  };
}

/** 警示条文案：数字齐全时含「已消耗 X / 预算 Y tokens」，否则诚实省略数字从句。 */
export function formatBudgetMessage(info: BudgetTripInfo): string {
  if (info.usedTokens !== null && info.tokenBudget !== null) {
    return `本次审计触发 Token 预算熔断，后续 LLM 阶段已降级：已消耗 ${info.usedTokens} / 预算 ${info.tokenBudget} tokens`;
  }
  return "本次审计触发 Token 预算熔断，后续 LLM 阶段已降级。";
}

// ----------------------------------------------------------------------
// 会话级「已确认降级任务」缓存：TaskDetail 从 SSE / 详情确认熔断后登记，
// Dashboard 列表仅对已登记的 done 行显示「预算降级」Tag。内存态、刷新即清空；
// 未登记的行不显示标记（不额外逐行请求 /summary 或详情，太重且契约无该字段）。
// ----------------------------------------------------------------------

const degradedCache = new Map<string, BudgetTripInfo>();

/** 登记一个已确认预算降级的任务（auditId 为空时忽略）。 */
export function rememberDegradedAudit(auditId: string, info: BudgetTripInfo): void {
  if (!auditId) return;
  degradedCache.set(auditId, info);
}

/** 查询任务是否已确认预算降级；未登记返回 null。 */
export function getDegradedAudit(auditId: string): BudgetTripInfo | null {
  return degradedCache.get(auditId) ?? null;
}

/** 清空缓存（测试用）。 */
export function resetDegradedCache(): void {
  degradedCache.clear();
}
