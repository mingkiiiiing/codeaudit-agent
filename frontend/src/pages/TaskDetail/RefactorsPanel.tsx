/**
 * 重构方案 Tab：listRefactors 拉取（契约 v1.7 RefactorProposal），卡片列表——
 * kind 标签（dedup 重复代码 / decompose 长函数分解 / split-module 模块拆分 /
 * simplify 简化 / other 其他）、目标、理由、收益、落地步骤、关联问题、来源与置信度。
 */

import { useCallback, useEffect, useState } from "react";
import { Alert, Card, Empty, Flex, Spin, Tag, Typography } from "antd";

import { ApiError, listRefactors } from "../../api/client";
import type { RefactorProposalItem } from "../../api/client";

const MONO_FONT = "Consolas, 'SFMono-Regular', Menlo, monospace";

const KIND_LABEL: Record<string, string> = {
  dedup: "重复代码聚类",
  decompose: "长函数分解",
  "split-module": "模块拆分",
  simplify: "简化",
  other: "其他",
};

const KIND_COLOR: Record<string, string> = {
  dedup: "purple",
  decompose: "blue",
  "split-module": "cyan",
  simplify: "geekblue",
  other: "default",
};

const SOURCE_LABEL: Record<string, string> = {
  heuristic: "启发式",
  llm: "LLM",
  "heuristic+llm": "启发式+LLM",
};

function ProposalCard({ proposal }: { proposal: RefactorProposalItem }) {
  const confidence =
    Number(proposal.confidence) > 0 ? `${Math.round(proposal.confidence * 100)}%` : "-";
  return (
    <Card
      size="small"
      title={proposal.title || proposal.id}
      extra={
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          置信度 {confidence}
        </Typography.Text>
      }
    >
      <Flex gap={8} wrap="wrap" align="center" style={{ marginBottom: 8 }}>
        <Tag color={KIND_COLOR[proposal.kind] ?? "default"}>
          {KIND_LABEL[proposal.kind] ?? proposal.kind}
        </Tag>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          来源：{SOURCE_LABEL[proposal.source] ?? proposal.source}
        </Typography.Text>
        <Typography.Text style={{ fontSize: 12.5 }}>
          目标：<Typography.Text code style={{ fontSize: 12 }}>{proposal.target || "-"}</Typography.Text>
        </Typography.Text>
      </Flex>

      {proposal.rationale && (
        <Typography.Paragraph style={{ marginBottom: 6 }}>{proposal.rationale}</Typography.Paragraph>
      )}
      {proposal.benefits && (
        <Typography.Paragraph style={{ marginBottom: 6 }}>
          <Typography.Text strong style={{ color: "#0f766e" }}>收益：</Typography.Text>
          {proposal.benefits}
        </Typography.Paragraph>
      )}

      {(proposal.steps?.length ?? 0) > 0 && (
        <>
          <Typography.Title level={5} style={{ fontSize: 13, marginBottom: 4 }}>
            落地步骤
          </Typography.Title>
          <ol style={{ margin: "0 0 8px", paddingLeft: 20, lineHeight: 1.7 }}>
            {proposal.steps.map((step, idx) => (
              <li key={idx}>{step}</li>
            ))}
          </ol>
        </>
      )}

      {(proposal.related_issues?.length ?? 0) > 0 && (
        <Flex gap={6} wrap="wrap" align="center">
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            关联问题：
          </Typography.Text>
          {proposal.related_issues.map((issueId) => (
            <Tag key={issueId} style={{ fontFamily: MONO_FONT, fontSize: 11 }}>
              {issueId}
            </Tag>
          ))}
        </Flex>
      )}
    </Card>
  );
}

export function RefactorsPanel({ auditId }: { auditId: string }) {
  const [proposals, setProposals] = useState<RefactorProposalItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const fetchRefactors = useCallback(async () => {
    try {
      const data = await listRefactors(auditId);
      setProposals(data.proposals);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : `加载重构方案失败：${String(err)}`);
    }
  }, [auditId]);

  useEffect(() => {
    void fetchRefactors();
  }, [fetchRefactors]);

  if (error) {
    return <Alert type="error" showIcon message="加载重构方案失败" description={error} />;
  }
  if (proposals === null) {
    return (
      <div style={{ textAlign: "center", padding: 32 }}>
        <Spin />
      </div>
    );
  }
  if (proposals.length === 0) {
    return <Empty description="该任务没有重构方案（需要先有架构索引与问题命中作为输入）" />;
  }

  return (
    <Flex vertical gap={12}>
      <Typography.Text type="secondary" style={{ fontSize: 12.5 }}>
        共 {proposals.length} 条重构方案
      </Typography.Text>
      {proposals.map((p) => (
        <ProposalCard key={p.id || `${p.target}-${p.kind}`} proposal={p} />
      ))}
    </Flex>
  );
}

export default RefactorsPanel;
