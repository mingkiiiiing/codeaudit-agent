/**
 * 问题列表 Tab：severity/category 走服务端过滤（listIssues），关键词前端过滤；
 * 表格行可展开显示详情卡（描述/证据/修复建议/代码片段）。
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  Flex,
  Input,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";

import { ApiError, listIssues } from "../../api/client";
import type { IssueItem } from "../../api/client";
import { SevTag } from "../../components/SevTag";
import { CAT_LABEL, SEV_LABEL } from "../../utils/format";
import {
  filterIssuesByKeyword,
  fmtConfidence,
  fmtLocation,
  FIX_COLOR,
  FIX_LABEL,
  sortIssues,
  SOURCE_LABEL,
} from "./issues";

const MONO_FONT = "Consolas, 'SFMono-Regular', Menlo, monospace";

const SEVERITY_OPTIONS = [
  { value: "", label: "全部严重度" },
  ...(["critical", "high", "medium", "low"] as const).map((sev) => ({
    value: sev,
    label: `${SEV_LABEL[sev] ?? sev} ${sev}`,
  })),
];

const CATEGORY_OPTIONS = [
  { value: "", label: "全部类别" },
  ...(["bug", "performance", "security", "style"] as const).map((cat) => ({
    value: cat,
    label: `${CAT_LABEL[cat] ?? cat} ${cat}`,
  })),
];

/** 修复状态展示（演示页 fixst 语义色）。 */
function FixStatus({ status }: { status: string }) {
  const label = FIX_LABEL[status] ?? status;
  return <span style={{ fontSize: 12, color: FIX_COLOR[status] ?? "#64748b" }}>{label}</span>;
}

/** 展开行详情卡（描述 / 证据列表 / 修复建议 / 代码片段）。 */
function IssueDetailCard({ issue }: { issue: IssueItem }) {
  const subtitle = [
    `#${issue.id}`,
    `来源：${SOURCE_LABEL[issue.source] ?? issue.source}`,
    issue.patch_id ? `补丁：${issue.patch_id}` : "",
  ]
    .filter(Boolean)
    .join("　");
  return (
    <div style={{ padding: "4px 8px 12px" }}>
      <Typography.Text type="secondary" style={{ fontFamily: MONO_FONT, fontSize: 12 }}>
        {subtitle}
      </Typography.Text>

      <Typography.Title level={5} style={{ marginTop: 10, marginBottom: 4, fontSize: 13 }}>
        描述
      </Typography.Title>
      <Typography.Paragraph style={{ marginBottom: 10 }}>
        {issue.description || "-"}
      </Typography.Paragraph>

      {(issue.evidence?.length ?? 0) > 0 && (
        <>
          <Typography.Title level={5} style={{ marginTop: 0, marginBottom: 4, fontSize: 13 }}>
            证据
          </Typography.Title>
          <ul style={{ margin: "0 0 10px", paddingLeft: 20, lineHeight: 1.6 }}>
            {issue.evidence.map((item, idx) => (
              <li key={idx}>{item}</li>
            ))}
          </ul>
        </>
      )}

      <Typography.Title level={5} style={{ marginTop: 0, marginBottom: 4, fontSize: 13 }}>
        修复建议
      </Typography.Title>
      <Typography.Paragraph style={{ marginBottom: 10 }}>
        {issue.suggestion || "-"}
      </Typography.Paragraph>

      {issue.code_snippet ? (
        <>
          <Typography.Title level={5} style={{ marginTop: 0, marginBottom: 4, fontSize: 13 }}>
            代码片段（{issue.file}:{issue.line_start}）
          </Typography.Title>
          <pre
            style={{
              margin: 0,
              background: "#0f172a",
              color: "#e2e8f0",
              borderRadius: 8,
              padding: "12px 14px",
              fontFamily: MONO_FONT,
              fontSize: 12.5,
              lineHeight: 1.5,
              overflowX: "auto",
            }}
          >
            {issue.code_snippet}
          </pre>
        </>
      ) : null}
    </div>
  );
}

export function IssuesPanel({ auditId }: { auditId: string }) {
  const [issues, setIssues] = useState<IssueItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [severity, setSeverity] = useState("");
  const [category, setCategory] = useState("");
  const [keyword, setKeyword] = useState("");

  const fetchIssues = useCallback(async () => {
    setLoading(true);
    try {
      const data = await listIssues(auditId, {
        severity: severity || undefined,
        category: category || undefined,
        limit: 1000,
        offset: 0,
      });
      setIssues(sortIssues(data.issues));
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : `加载问题列表失败：${String(err)}`);
    } finally {
      setLoading(false);
    }
  }, [auditId, severity, category]);

  useEffect(() => {
    void fetchIssues();
  }, [fetchIssues]);

  const filtered = useMemo(
    () => filterIssuesByKeyword(issues ?? [], keyword),
    [issues, keyword],
  );

  const columns: ColumnsType<IssueItem> = [
    {
      title: "严重度",
      dataIndex: "severity",
      key: "severity",
      width: 90,
      render: (sev: string) => <SevTag severity={sev} />,
    },
    {
      title: "类别",
      dataIndex: "category",
      key: "category",
      width: 90,
      render: (cat: string) => <Tag>{CAT_LABEL[cat] ?? cat}</Tag>,
    },
    {
      title: "文件:行",
      key: "location",
      width: 240,
      ellipsis: true,
      render: (_, record) => (
        <Tooltip title={fmtLocation(record)}>
          <span style={{ fontFamily: MONO_FONT, fontSize: 12 }}>{fmtLocation(record)}</span>
        </Tooltip>
      ),
    },
    { title: "标题", dataIndex: "title", key: "title" },
    {
      title: "置信度",
      dataIndex: "confidence",
      key: "confidence",
      width: 80,
      render: (c: number) => fmtConfidence(c),
    },
    {
      title: "修复状态",
      dataIndex: "fix_status",
      key: "fix_status",
      width: 110,
      render: (status: string) => <FixStatus status={status} />,
    },
  ];

  if (error) {
    return <Alert type="error" showIcon message="加载问题列表失败" description={error} />;
  }

  return (
    <Flex vertical gap={12}>
      <Space wrap>
        <Select
          value={severity}
          onChange={setSeverity}
          options={SEVERITY_OPTIONS}
          style={{ width: 150 }}
          aria-label="严重度过滤"
        />
        <Select
          value={category}
          onChange={setCategory}
          options={CATEGORY_OPTIONS}
          style={{ width: 140 }}
          aria-label="类别过滤"
        />
        <Input
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
          placeholder="关键词：标题 / 文件 / 描述 / ID"
          style={{ width: 260 }}
          allowClear
        />
        <Typography.Text type="secondary" style={{ fontSize: 12.5 }}>
          {issues === null ? "加载中 …" : `显示 ${filtered.length} / ${issues.length} 条`}
        </Typography.Text>
      </Space>

      {issues === null ? (
        <div style={{ textAlign: "center", padding: 32 }}>
          <Spin />
        </div>
      ) : (
        <Table<IssueItem>
          rowKey="id"
          size="small"
          columns={columns}
          dataSource={filtered}
          loading={loading}
          pagination={{
            pageSize: 10,
            hideOnSinglePage: true,
            showSizeChanger: false,
            showTotal: (total) => `共 ${total} 条`,
          }}
          expandable={{
            expandedRowRender: (record) => <IssueDetailCard issue={record} />,
            rowExpandable: () => true,
          }}
          locale={{
            emptyText: keyword.trim() ? "没有符合条件的问题" : "该任务没有问题记录",
          }}
        />
      )}
    </Flex>
  );
}

export default IssuesPanel;
