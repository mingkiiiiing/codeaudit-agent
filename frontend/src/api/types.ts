/**
 * API 契约类型（docs/13 §3/§6；字段名与后端 audit/models.to_dict() 完全一致，不做 camel 转换）。
 * 以 server/app.py 与 audit/models.py 为 source of truth。
 */

/** SSE 事件帧（{stage,message,error} 及终帧 {type:"done"}），见 web/index.html 事件流处理。 */
export interface AuditEvent {
  type?: string;
  stage?: string;
  message?: string;
  error?: string | boolean;
  [key: string]: unknown;
}

/** GET /api/audits 列表条目（created_at/source_path/do_fix/do_tests 为 v2 新增字段）。 */
export interface TaskListItem {
  audit_id: string;
  status: string;
  created_at?: string | null;
  source_path?: string | null;
  do_fix?: boolean;
  do_tests?: boolean;
  error?: string | null;
}

/** GET /api/audits/{id}：任务详情（done 时附带 report 原始结构）。 */
export interface AuditDetail {
  audit_id: string;
  status: string;
  error?: string | null;
  created_at?: string | null;
  source_path?: string | null;
  do_fix?: boolean;
  do_tests?: boolean;
  report?: Record<string, unknown> | null;
}

/** 问题条目（audit.models.Issue.to_dict()）。 */
export interface IssueItem {
  id: string;
  severity: string;
  category: string;
  file: string;
  line_start: number;
  line_end: number;
  title: string;
  description: string;
  suggestion: string;
  evidence: string[];
  code_snippet: string;
  confidence: number;
  source: string;
  fix_status: string;
  patch_id: string;
}

/** 修复补丁（audit.models.Patch.to_dict()）。 */
export interface PatchItem {
  id: string;
  issue_id: string;
  diff: string;
  apply_status: string;
  rationale: string;
  tests_run: number;
  tests_passed: number;
}

/** 重构方案（audit.models.RefactorProposal.to_dict()，契约 v1.7 字段）。 */
export interface RefactorProposalItem {
  id: string;
  title: string;
  target: string;
  kind: string;
  rationale: string;
  steps: string[];
  benefits: string;
  related_issues: string[];
  source: string;
  confidence: number;
}

/** 架构理解卡片（report.architecture.to_dict()，结构宽松处理）。 */
export type ArchitectureCard = Record<string, unknown>;

/** GET /api/audits/{id}/summary：仪表盘头摘要。 */
export interface AuditSummary {
  audit_id: string;
  project_name: string;
  health_score: number;
  summary: Record<string, number>;
  total_issues: number;
  loc: number;
  duration_sec: number;
  tokens: {
    llm_calls: number;
    prompt_tokens: number;
    completion_tokens: number;
    cache_hits: number;
    cache_misses: number;
  };
}

/** POST /api/audits 与 POST /api/audits/upload 的响应。 */
export interface CreateAuditResult {
  audit_id: string;
}

/** GET /api/health。 */
export interface HealthInfo {
  status: string;
  version: string;
  audits: {
    active: number;
    total: number;
  };
}
