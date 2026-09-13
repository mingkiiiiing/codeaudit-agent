/**
 * 修复补丁 Tab：listPatches 拉取，卡片头展示补丁元信息，DiffView 渲染 diff。
 * apply_status 语义色沿用演示页 .patch-head .st：verified 绿 / needs-review 黄 /
 * syntax-ok 蓝 / failed 红 / pending 灰。
 */

import { useCallback, useEffect, useState } from "react";
import { Alert, Card, Empty, Flex, Spin, Tag, Tooltip, Typography } from "antd";

import { ApiError, listPatches } from "../../api/client";
import type { PatchItem } from "../../api/client";
import { DiffView } from "./DiffView";

const APPLY_LABEL: Record<string, string> = {
  pending: "待处理",
  verified: "已验证",
  "needs-review": "待复核",
  "syntax-ok": "语法通过",
  failed: "失败",
};

const APPLY_COLOR: Record<string, string> = {
  pending: "default",
  verified: "green",
  "needs-review": "gold",
  "syntax-ok": "blue",
  failed: "red",
};

export function PatchesPanel({ auditId }: { auditId: string }) {
  const [patches, setPatches] = useState<PatchItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const fetchPatches = useCallback(async () => {
    try {
      const data = await listPatches(auditId);
      setPatches(data.patches);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : `加载补丁列表失败：${String(err)}`);
    }
  }, [auditId]);

  useEffect(() => {
    void fetchPatches();
  }, [fetchPatches]);

  if (error) {
    return <Alert type="error" showIcon message="加载补丁列表失败" description={error} />;
  }
  if (patches === null) {
    return (
      <div style={{ textAlign: "center", padding: 32 }}>
        <Spin />
      </div>
    );
  }
  if (patches.length === 0) {
    return <Empty description="未生成补丁（未启用修复，或没有可修复的问题）" />;
  }

  return (
    <Flex vertical gap={14}>
      <Typography.Text type="secondary" style={{ fontSize: 12.5 }}>
        共 {patches.length} 个补丁
      </Typography.Text>
      {patches.map((p) => (
        <Card
          key={p.id}
          size="small"
          styles={{ body: { padding: 0, overflow: "hidden" } }}
          title={
            <Flex gap={12} wrap="wrap" align="center">
              <Typography.Text code style={{ fontSize: 12.5 }}>
                {p.id}
              </Typography.Text>
              <Typography.Text type="secondary" style={{ fontSize: 12.5 }}>
                关联问题：<Typography.Text code style={{ fontSize: 12 }}>{p.issue_id || "-"}</Typography.Text>
              </Typography.Text>
              <Tooltip title={`测试通过 ${p.tests_passed} / ${p.tests_run}`}>
                <Typography.Text style={{ fontSize: 12.5 }}>
                  测试：{p.tests_passed} / {p.tests_run}
                </Typography.Text>
              </Tooltip>
              <Tag color={APPLY_COLOR[p.apply_status] ?? "default"}>
                {APPLY_LABEL[p.apply_status] ?? p.apply_status}
              </Tag>
            </Flex>
          }
        >
          {p.rationale && (
            <Typography.Paragraph
              type="secondary"
              style={{ margin: "10px 16px 4px", fontSize: 12.5 }}
            >
              {p.rationale}
            </Typography.Paragraph>
          )}
          <div style={{ marginTop: p.rationale ? 4 : 0 }}>
            <DiffView diff={p.diff} />
          </div>
        </Card>
      ))}
    </Flex>
  );
}

export default PatchesPanel;
