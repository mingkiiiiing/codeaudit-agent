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

/** GET /api/audits/{id}/reports 列表条目：报告历史版本摘要（W27-B，seq 升序，不含报告全文）。 */
export interface ReportVersionSummary {
  /** 版本号（从 1 起；当前报告即历史最大 seq）。 */
  seq: number;
  /** 版本生成时间（ISO 字符串）。 */
  created_at: string;
  /** 该版本健康分。 */
  health_score: number;
  /** 该版本问题总数。 */
  issue_count: number;
}

/** GET /api/audits/{id}/reports 响应（total + 版本摘要列表；任务不存在 404）。 */
export interface ReportHistoryList {
  total: number;
  reports: ReportVersionSummary[];
}

/**
 * GET /api/audits/{id}/reports/{seq}：指定历史版本完整报告 JSON
 * （audit.models.AuditReport.to_dict() 同源形状，与当前报告端点 /report?format=json 一致）。
 * 报告历史面板只消费健康分/严重度计数等关键字段，其余字段以索引签名宽松保留。
 */
export interface ReportVersionDetail {
  audit_id: string;
  project_name: string;
  /** 语言 → 占比。 */
  languages: Record<string, number>;
  loc: number;
  health_score: number;
  /** severity → count（critical/high/medium/low）。 */
  summary: Record<string, number>;
  created_at: string;
  schema_version: string;
  [key: string]: unknown;
}

/** POST /api/rename 请求体（W28-B safe-rename 符号重命名，字段与后端 RenameRequest 一致）。 */
export interface RenameRequest {
  /** 待重命名扫描范围（单 .py 文件或目录，目录时递归）。 */
  source_path: string;
  /** 现名（须为合法 Python 标识符且非关键字）。 */
  old_name: string;
  /** 新名（合法 Python 标识符、非关键字，且 != old_name）。 */
  new_name: string;
  /** true=写入源码；false/缺省=dry-run 预览不落盘。 */
  apply?: boolean;
  /** 目标语言，缺省 "python"（前端不传，由后端取缺省值）。 */
  language?: string;
}

/** POST /api/rename 响应（dry-run 与 apply 同形状，applied 区分是否落盘）。 */
export interface RenameResult {
  ok: boolean;
  /** false=dry-run（未落盘）；true=已写入源码。 */
  applied: boolean;
  /** 受影响文件路径列表（即计划补丁覆盖的文件）。 */
  files: string[];
  /** 全部替换点总数。 */
  replace_points: number;
  /** 逐文件 unified diff 文本（键为文件路径）。 */
  diffs: Record<string, string>;
  errors: string[];
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
