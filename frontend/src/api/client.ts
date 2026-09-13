/**
 * API 公共层：契约化封装 docs/13 §3 的全部 REST 端点 + SSE 订阅（含降级轮询）。
 * 基路径为 ""（同源部署；开发态走 vite proxy → 127.0.0.1:8000）。
 */

import type {
  ArchitectureCard,
  AuditDetail,
  AuditEvent,
  AuditSummary,
  CreateAuditResult,
  HealthInfo,
  IssueItem,
  PatchItem,
  RefactorProposalItem,
  TaskListItem,
} from "./types";

export type {
  ArchitectureCard,
  AuditDetail,
  AuditEvent,
  AuditSummary,
  CreateAuditResult,
  HealthInfo,
  IssueItem,
  PatchItem,
  RefactorProposalItem,
  TaskListItem,
} from "./types";

/** 非 2xx（或网络失败）时抛出：detail 为响应体 {"detail": ...} 的解析结果。 */
export class ApiError extends Error {
  readonly detail: string;
  readonly status: number;

  constructor(detail: string, status: number) {
    super(detail);
    this.name = "ApiError";
    this.detail = detail;
    this.status = status;
  }
}

type QueryValue = string | number | boolean | undefined | null;

function buildUrl(path: string, query?: Record<string, QueryValue>): string {
  if (!query) return path;
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === "") continue;
    params.set(key, String(value));
  }
  const qs = params.toString();
  return qs ? `${path}?${qs}` : path;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let resp: Response;
  try {
    resp = await fetch(path, init);
  } catch (err) {
    throw new ApiError(`网络请求失败：${String(err)}`, 0);
  }
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`;
    try {
      const data = (await resp.json()) as { detail?: unknown };
      if (data && typeof data.detail === "string") detail = data.detail;
      else if (data && data.detail != null) detail = JSON.stringify(data.detail);
    } catch {
      // 响应体不是 JSON：保留默认 detail
    }
    throw new ApiError(detail, resp.status);
  }
  if (resp.status === 204) return undefined as T;
  return (await resp.json()) as T;
}

// ---------------------------------------------------------------------- REST

export function getHealth(): Promise<HealthInfo> {
  return request<HealthInfo>("/api/health");
}

export function listAudits(
  limit?: number,
  offset?: number,
): Promise<{ total: number; audits: TaskListItem[] }> {
  return request(buildUrl("/api/audits", { limit, offset }));
}

export function createAudit(body: {
  source_path: string;
  do_fix: boolean;
  do_tests: boolean;
}): Promise<CreateAuditResult> {
  return request<CreateAuditResult>("/api/audits", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function uploadAuditZip(
  file: File,
  opts: { do_fix: boolean; do_tests: boolean },
): Promise<CreateAuditResult> {
  const form = new FormData();
  form.append("file", file);
  form.append("do_fix", String(opts.do_fix));
  form.append("do_tests", String(opts.do_tests));
  return request<CreateAuditResult>("/api/audits/upload", { method: "POST", body: form });
}

export function getAudit(id: string): Promise<AuditDetail> {
  return request<AuditDetail>(`/api/audits/${encodeURIComponent(id)}`);
}

export async function deleteAudit(id: string): Promise<void> {
  await request<void>(`/api/audits/${encodeURIComponent(id)}`, { method: "DELETE" });
}

export function getSummary(id: string): Promise<AuditSummary> {
  return request<AuditSummary>(`/api/audits/${encodeURIComponent(id)}/summary`);
}

export interface ListIssuesParams {
  severity?: string;
  category?: string;
  limit?: number;
  offset?: number;
}

export function listIssues(
  id: string,
  params?: ListIssuesParams,
): Promise<{ total: number; issues: IssueItem[] }> {
  const p = params ?? {};
  return request(buildUrl(`/api/audits/${encodeURIComponent(id)}/issues`, {
    severity: p.severity,
    category: p.category,
    limit: p.limit,
    offset: p.offset,
  }));
}

export function listPatches(id: string): Promise<{ total: number; patches: PatchItem[] }> {
  return request(`/api/audits/${encodeURIComponent(id)}/patches`);
}

export function listRefactors(
  id: string,
): Promise<{ total: number; proposals: RefactorProposalItem[] }> {
  return request(`/api/audits/${encodeURIComponent(id)}/refactors`);
}

export function getUnderstand(id: string): Promise<{ architecture: ArchitectureCard | null }> {
  return request(`/api/audits/${encodeURIComponent(id)}/understand`);
}

export function reportUrl(id: string, fmt: "json" | "md" | "html"): string {
  return `/api/audits/${encodeURIComponent(id)}/report?format=${fmt}`;
}

// ---------------------------------------------------------------------- SSE

/**
 * 订阅任务事件流（EventSource 优先）：
 * - onmessage 里 JSON.parse，失败静默忽略；
 * - 收到 {type:"done"} 终帧 → 关闭连接并回调 onDone；
 * - 其余事件 → onEvent(obj)；
 * - onerror → 关闭 EventSource，降级为每 1s getAudit 轮询，
 *   直到 status 为 done/failed → onDone；轮询遇 404 → onError；
 *   其他错误继续轮询（沿用 web/index.html 已验证模式）。
 *
 * 返回清理函数：关闭 EventSource 并清除轮询 interval（幂等，调用后不再触发任何回调）。
 */
export function subscribeEvents(
  id: string,
  onEvent: (event: AuditEvent) => void,
  onDone: () => void,
  onError: (err: unknown) => void,
): () => void {
  let stopped = false;
  let es: EventSource | null = null;
  let timer: ReturnType<typeof setInterval> | null = null;

  const stopAll = (): void => {
    stopped = true;
    if (es) {
      es.close();
      es = null;
    }
    if (timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  };

  const startPolling = (): void => {
    if (stopped || timer !== null) return;
    timer = setInterval(() => {
      void (async () => {
        if (stopped) return;
        let detail: AuditDetail;
        try {
          detail = await getAudit(id);
        } catch (err) {
          if (stopped) return;
          if (err instanceof ApiError && err.status === 404) {
            stopAll();
            onError(err);
          }
          return; // 其他错误：保持轮询
        }
        if (stopped) return;
        if (detail.status === "done" || detail.status === "failed") {
          stopAll();
          onDone();
        }
      })();
    }, 1000);
  };

  es = new EventSource(`/api/audits/${encodeURIComponent(id)}/events`);
  es.onmessage = (ev: MessageEvent<string>) => {
    let event: AuditEvent;
    try {
      event = JSON.parse(ev.data) as AuditEvent;
    } catch {
      return; // 非 JSON 帧：忽略
    }
    if (event && event.type === "done") {
      stopAll();
      onDone();
      return;
    }
    if (event) onEvent(event);
  };
  es.onerror = () => {
    if (stopped) return;
    if (es) {
      es.close();
      es = null;
    }
    startPolling();
  };

  return stopAll;
}
