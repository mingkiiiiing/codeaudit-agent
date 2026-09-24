/**
 * 报告历史面板（W29 卡D）：W27-B 结果版本化的前端呈现。
 * 数据源：
 * - GET /api/audits/{id}/reports → {total, reports:[{seq,created_at,health_score,issue_count}]}（seq 升序）；
 * - GET /api/audits/{id}/reports/{seq} → 该版本完整报告 JSON（展开时拉取，仅展示健康分/严重度计数等关键摘要）。
 * 降级约定：列表为空、404 或任何接口失败 → 统一显示「暂无历史版本」空态，不弹错误阻断页面；
 * 版本详情加载失败 → 行内静默降级文案（诚实提示，不伪造数据）。
 */

import { Collapse, Empty, Flex, Space, Spin, Typography } from "antd";
import { useEffect, useState } from "react";

import { getReport, listReports } from "../api/client";
import type { ReportVersionDetail, ReportVersionSummary } from "../api/client";
import { fmtDateTime } from "../utils/format";
import { SevTag } from "./SevTag";

const MONO_FONT = "Consolas, 'SFMono-Regular', Menlo, monospace";
const SEVERITIES = ["critical", "high", "medium", "low"] as const;

/** 健康分配色（阈值与 OverviewHead.healthColor 对齐：≥85 绿 / ≥70 黄 / ≥50 橙 / <50 红）。 */
function historyHealthColor(score: number): string {
  if (score >= 85) return "#16a34a";
  if (score >= 70) return "#ca8a04";
  if (score >= 50) return "#ea580c";
  return "#b91c1c";
}

/** Collapse 头部的版本摘要行：版本号 / 生成时间 / 健康分 / 问题数。 */
function VersionLabel({ version }: { version: ReportVersionSummary }) {
  const score = Number(version.health_score) || 0;
  return (
    <Space size={14} wrap>
      <Typography.Text strong style={{ fontFamily: MONO_FONT }}>
        版本 {version.seq}
      </Typography.Text>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        {fmtDateTime(version.created_at)}
      </Typography.Text>
      <Typography.Text
        style={{ color: historyHealthColor(score), fontWeight: 600, fontFamily: MONO_FONT }}
      >
        健康分 {score}
      </Typography.Text>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        问题 {version.issue_count}
      </Typography.Text>
    </Space>
  );
}

/** 展开后的单版本关键摘要：健康分 / 四级严重度计数 / 代码行数 / 生成时间。 */
function VersionDetailPanel({ auditId, seq }: { auditId: string; seq: number }) {
  const [detail, setDetail] = useState<ReportVersionDetail | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setDetail(null);
    setFailed(false);
    getReport(auditId, seq)
      .then((data) => {
        if (!cancelled) setDetail(data);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, [auditId, seq]);

  if (failed) {
    return (
      <Typography.Text type="secondary" style={{ fontSize: 12.5 }}>
        该版本摘要加载失败，可收起后重新展开重试。
      </Typography.Text>
    );
  }
  if (detail === null) {
    return (
      <div style={{ textAlign: "center", padding: 16 }}>
        <Spin />
      </div>
    );
  }

  const summary = detail.summary ?? {};
  return (
    <Flex vertical gap={8} data-testid={`report-version-${seq}`}>
      <Space size={16} wrap>
        <Typography.Text style={{ fontSize: 12.5 }}>
          健康分{" "}
          <Typography.Text
            strong
            style={{
              color: historyHealthColor(Number(detail.health_score) || 0),
              fontFamily: MONO_FONT,
            }}
          >
            {detail.health_score}
          </Typography.Text>{" "}
          / 100
        </Typography.Text>
        {SEVERITIES.map((sev) => (
          <span key={sev} style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
            <SevTag severity={sev} />
            <Typography.Text style={{ fontSize: 12.5 }}>{summary[sev] ?? 0}</Typography.Text>
          </span>
        ))}
      </Space>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        代码行数 {detail.loc} · 生成于 {fmtDateTime(detail.created_at)}
      </Typography.Text>
    </Flex>
  );
}

/**
 * 报告历史面板内容（TaskDetail 页以 Card title="报告历史" 包裹）：
 * 挂载即拉取版本列表；点击某版本展开该 seq 的关键摘要。
 */
export function ReportHistory({ auditId }: { auditId: string }) {
  const [versions, setVersions] = useState<ReportVersionSummary[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    setVersions(null);
    listReports(auditId)
      .then((data) => {
        // 防御：reports 缺字段按空列表处理
        if (!cancelled) setVersions(Array.isArray(data?.reports) ? data.reports : []);
      })
      .catch(() => {
        // 列表为空 / 404 / 任何接口失败：统一降级为空态文案，不阻断页面
        if (!cancelled) setVersions([]);
      });
    return () => {
      cancelled = true;
    };
  }, [auditId]);

  if (versions === null) {
    return (
      <div style={{ textAlign: "center", padding: 24 }}>
        <Spin />
      </div>
    );
  }
  if (versions.length === 0) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无历史版本" />;
  }

  return (
    <Flex vertical gap={8}>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        共 {versions.length} 个历史版本（当前报告即最新版本）
      </Typography.Text>
      <Collapse
        items={versions.map((version) => ({
          key: String(version.seq),
          label: <VersionLabel version={version} />,
          children: <VersionDetailPanel auditId={auditId} seq={version.seq} />,
        }))}
      />
    </Flex>
  );
}

export default ReportHistory;
