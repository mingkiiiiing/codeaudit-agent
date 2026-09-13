/**
 * 问题列表的纯逻辑：前端关键词过滤、排序、展示格式化。
 * 规则沿用 web/index.html 演示页（severity/category 服务端过滤在 IssuesPanel 内完成）。
 */

import type { IssueItem } from "../../api/types";

/** 严重度排序权重（演示页 SEV_RANK）。 */
export const SEV_RANK: Record<string, number> = { critical: 0, high: 1, medium: 2, low: 3 };

/** 修复状态文案（演示页 FIX_LABEL）。 */
export const FIX_LABEL: Record<string, string> = {
  none: "未修复",
  patch_generated: "已生成补丁",
  verified: "已验证",
  "needs-review": "待人工复核",
  "syntax-ok": "语法校验通过",
};

/** 修复状态展示色（对齐演示页 .fixst 语义：verified 绿 / needs-review 橙 / 补丁系蓝 / 无灰）。 */
export const FIX_COLOR: Record<string, string> = {
  none: "#64748b",
  patch_generated: "#1d4ed8",
  verified: "#16a34a",
  "needs-review": "#b45309",
  "syntax-ok": "#1d4ed8",
};

/** 检测来源文案（演示页 detailCard）。 */
export const SOURCE_LABEL: Record<string, string> = {
  rule: "规则",
  llm: "LLM",
  "rule+llm": "规则+LLM",
};

/** 前端关键词过滤：匹配 id / 标题 / 文件 / 描述 / 修复建议，大小写不敏感（与演示页字段一致）。 */
export function filterIssuesByKeyword(issues: IssueItem[], keyword: string): IssueItem[] {
  const kw = keyword.trim().toLowerCase();
  if (!kw) return issues;
  return issues.filter((issue) =>
    [issue.id, issue.title, issue.file, issue.description, issue.suggestion].some((value) =>
      String(value ?? "").toLowerCase().includes(kw),
    ),
  );
}

/** 排序：严重度 rank → 文件名 → 起始行（演示页 fetchIssues 内排序规则）。 */
export function sortIssues(issues: IssueItem[]): IssueItem[] {
  return issues.slice().sort(
    (a, b) =>
      (SEV_RANK[a.severity] ?? 9) - (SEV_RANK[b.severity] ?? 9) ||
      (a.file || "").localeCompare(b.file || "") ||
      (a.line_start || 0) - (b.line_start || 0),
  );
}

/** 位置展示：file:line_start[-line_end]。 */
export function fmtLocation(issue: IssueItem): string {
  const range = issue.line_end > issue.line_start ? `-${issue.line_end}` : "";
  return `${issue.file}:${issue.line_start}${range}`;
}

/** 置信度 0~1 → 百分比整数（演示页 Math.round(c*100)%）。 */
export function fmtConfidence(confidence: number): string {
  return `${Math.round((Number(confidence) || 0) * 100)}%`;
}
