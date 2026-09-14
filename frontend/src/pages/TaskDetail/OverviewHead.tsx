/**
 * 完成后的仪表盘头部：健康分 gauge（echarts）+ 严重度分布 bar + 四个严重度徽章
 * + 元信息 + 报告下载链接组。配色沿用演示页（≥85 绿 / ≥70 黄 / ≥50 橙 / <50 红）。
 * Token 消耗字段（W13-A4）：llm_calls/prompt/completion/缓存各带 tooltip 解释，
 * 缓存行附命中率（命中率 = 命中 /（命中 + 未命中））。
 */

import { Divider, Space, Tooltip, Typography } from "antd";
import type { EChartsOption } from "echarts";

import { reportUrl } from "../../api/client";
import type { AuditSummary } from "../../api/client";
import { fmtDuration, sevColor, SEV_LABEL } from "../../utils/format";
import { useChart } from "./useChart";

const SEVERITIES = ["critical", "high", "medium", "low"] as const;
const MONO_FONT = "Consolas, 'SFMono-Regular', Menlo, monospace";

/** 健康分配色（演示页 healthClass：good 绿 / fair 黄 / poor 橙 / bad 红）。 */
export function healthColor(score: number): string {
  if (score >= 85) return "#16a34a";
  if (score >= 70) return "#ca8a04";
  if (score >= 50) return "#ea580c";
  return "#b91c1c";
}

/** 健康等级文案（演示页 healthLevel）。 */
export function healthLevel(score: number): string {
  if (score >= 85) return "良好";
  if (score >= 70) return "一般";
  if (score >= 50) return "较差";
  return "病态";
}

/** 健康分 gauge option。 */
export function gaugeOption(score: number): EChartsOption {
  const color = healthColor(score);
  return {
    series: [
      {
        type: "gauge",
        startAngle: 210,
        endAngle: -30,
        min: 0,
        max: 100,
        progress: { show: true, width: 14, itemStyle: { color } },
        axisLine: { lineStyle: { width: 14, color: [[1, "#e2e8f0"]] } },
        axisTick: { show: false },
        splitLine: { show: false },
        axisLabel: { show: false },
        pointer: { show: false },
        anchor: { show: false },
        title: { show: false },
        detail: {
          offsetCenter: [0, 0],
          fontSize: 30,
          fontWeight: 700,
          color,
          formatter: "{value}",
        },
        data: [{ value: Number(score) || 0 }],
      },
    ],
  };
}

/** 严重度分布 bar option（各柱使用严重度语义色）。 */
export function severityBarOption(summary: AuditSummary): EChartsOption {
  return {
    grid: { left: 8, right: 8, top: 24, bottom: 0, containLabel: true },
    xAxis: {
      type: "category",
      data: SEVERITIES.map((sev) => SEV_LABEL[sev]),
      axisLabel: { fontSize: 11, color: "#64748b" },
      axisLine: { lineStyle: { color: "#e2e8f0" } },
    },
    yAxis: { type: "value", minInterval: 1, axisLabel: { fontSize: 11, color: "#64748b" } },
    series: [
      {
        type: "bar",
        barWidth: 26,
        data: SEVERITIES.map((sev) => ({
          value: summary.summary?.[sev] ?? 0,
          itemStyle: { color: sevColor(sev), borderRadius: [3, 3, 0, 0] },
        })),
        label: { show: true, position: "top", fontSize: 11, color: "#475569" },
      },
    ],
  };
}

function MetaItem({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ display: "flex", gap: 6, fontSize: 12.5 }}>
      <span style={{ color: "#64748b", flexShrink: 0 }}>{label}</span>
      <span style={{ color: "#1e293b", fontWeight: 600, wordBreak: "break-all" }}>{children}</span>
    </div>
  );
}

export function OverviewHead({ summary }: { summary: AuditSummary }) {
  const score = Number(summary.health_score) || 0;
  const gaugeRef = useChart(() => gaugeOption(score), [score]);
  const barRef = useChart(
    () => severityBarOption(summary),
    [summary],
  );
  const tokens = summary.tokens;
  const cacheTotal = (tokens?.cache_hits ?? 0) + (tokens?.cache_misses ?? 0);
  const cacheRate =
    cacheTotal > 0 ? `${(((tokens?.cache_hits ?? 0) / cacheTotal) * 100).toFixed(1)}%` : null;

  return (
    <div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 24, alignItems: "stretch" }}>
        {/* 健康分 gauge + 文本兜底（图挂了信息也不丢） */}
        <div style={{ width: 200, flexShrink: 0 }}>
          <div ref={gaugeRef} style={{ width: "100%", height: 150 }} />
          <Typography.Text style={{ color: healthColor(score), fontWeight: 600 }}>
            健康分 · {healthLevel(score)} · {score}/100
          </Typography.Text>
        </div>

        {/* 严重度分布 bar */}
        <div style={{ width: 260, flexShrink: 0 }}>
          <div ref={barRef} style={{ width: "100%", height: 186 }} />
        </div>

        {/* 四个严重度徽章 */}
        <div style={{ display: "flex", flexWrap: "wrap", gap: 10, alignContent: "center", flex: "1 1 240px" }}>
          {SEVERITIES.map((sev) => (
            <div
              key={sev}
              style={{
                minWidth: 104,
                padding: "10px 14px",
                borderRadius: 8,
                color: "#fff",
                background: sevColor(sev),
              }}
            >
              <div style={{ fontSize: 12, opacity: 0.92 }}>
                {SEV_LABEL[sev]} {sev}
              </div>
              <div style={{ fontSize: 24, fontWeight: 700, marginTop: 2 }}>
                {summary.summary?.[sev] ?? 0}
              </div>
            </div>
          ))}
        </div>

        {/* 元信息 */}
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "auto auto",
            gap: "4px 18px",
            alignContent: "start",
          }}
        >
          <MetaItem label="项目名">{summary.project_name || "-"}</MetaItem>
          <MetaItem label="代码行数">{summary.loc}</MetaItem>
          <MetaItem label="耗时">{fmtDuration(summary.duration_sec)}</MetaItem>
          <MetaItem label="LLM 调用">
            <Tooltip title="llm_calls：本次审计的 LLM（模型）调用次数，各阶段累计。">
              <span>{tokens?.llm_calls ?? 0}</span>
            </Tooltip>
          </MetaItem>
          <MetaItem label="Prompt tokens">
            <Tooltip title="prompt_tokens：发送给模型的输入 token 总量。">
              <span>{tokens?.prompt_tokens ?? 0}</span>
            </Tooltip>
          </MetaItem>
          <MetaItem label="Completion tokens">
            <Tooltip title="completion_tokens：模型生成的输出 token 总量。">
              <span>{tokens?.completion_tokens ?? 0}</span>
            </Tooltip>
          </MetaItem>
          <MetaItem label="缓存命中/未命中">
            <Tooltip title="cache_hits / cache_misses：Prompt 缓存命中与未命中次数；命中率 = 命中 ÷（命中 + 未命中）。">
              <span>
                {tokens?.cache_hits ?? 0} / {tokens?.cache_misses ?? 0}
                {cacheRate ? `（命中率 ${cacheRate}）` : ""}
              </span>
            </Tooltip>
          </MetaItem>
          <MetaItem label="审计 ID">
            <span style={{ fontFamily: MONO_FONT }}>{summary.audit_id}</span>
          </MetaItem>
        </div>
      </div>

      <Divider style={{ margin: "14px 0 10px" }} />
      <Space size={16} wrap>
        <Typography.Text type="secondary">报告下载：</Typography.Text>
        <a href={reportUrl(summary.audit_id, "html")} target="_blank" rel="noreferrer">
          report.html
        </a>
        <a href={reportUrl(summary.audit_id, "md")} target="_blank" rel="noreferrer">
          report.md
        </a>
        <a href={reportUrl(summary.audit_id, "json")} target="_blank" rel="noreferrer">
          report.json
        </a>
      </Space>
    </div>
  );
}

export default OverviewHead;
