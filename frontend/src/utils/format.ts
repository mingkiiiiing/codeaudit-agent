/**
 * 展示格式化工具：颜色/文案语义沿用 web/index.html 演示页。
 */

import dayjs from "dayjs";

/** 严重度 → 品牌语义色（critical 红 / high 橙 / medium 黄 / low 蓝）。 */
export function sevColor(sev: string): string {
  switch (sev) {
    case "critical":
      return "#b91c1c";
    case "high":
      return "#ea580c";
    case "medium":
      return "#ca8a04";
    case "low":
      return "#2563eb";
    default:
      return "#64748b";
  }
}

export const SEV_LABEL: Record<string, string> = {
  critical: "致命",
  high: "严重",
  medium: "中等",
  low: "轻微",
};

export const CAT_LABEL: Record<string, string> = {
  bug: "缺陷",
  performance: "性能",
  style: "规范",
  security: "安全",
};

export const STATUS_LABEL: Record<string, string> = {
  queued: "排队中",
  running: "进行中",
  done: "已完成",
  failed: "失败",
};

/** 取审计 ID 前 8 位用于列表展示。 */
export function shortId(id: string): string {
  return (id || "").slice(0, 8);
}

/** 耗时：<60s 显示 "x.xx s"，否则 "x 分 y 秒"。 */
export function fmtDuration(sec: number): string {
  const s = Number(sec) || 0;
  if (s < 60) return `${s.toFixed(2)} s`;
  const minutes = Math.floor(s / 60);
  const rest = Math.round(s % 60);
  return `${minutes} 分 ${rest} 秒`;
}

/** ISO 时间 → "YYYY-MM-DD HH:mm:ss"；空值/非法值返回 "-"。 */
export function fmtDateTime(iso?: string | null): string {
  if (!iso) return "-";
  const d = dayjs(iso);
  return d.isValid() ? d.format("YYYY-MM-DD HH:mm:ss") : "-";
}

/** 字节数 → 人类可读大小（用于 zip 上传回显）。 */
export function fmtFileSize(bytes: number): string {
  const b = Number(bytes) || 0;
  if (b < 1024) return `${b} B`;
  if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)} KB`;
  return `${(b / 1024 / 1024).toFixed(2)} MB`;
}
