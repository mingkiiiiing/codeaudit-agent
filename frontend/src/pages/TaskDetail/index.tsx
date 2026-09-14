/**
 * 任务详情页（/audits/:id）：
 * - queued/running：七阶段 Steps 进度 + SSE 实时事件（subscribeEvents，断流自动降级轮询）；
 * - done：健康分 gauge 与严重度分布 + Tabs（问题列表 / 修复补丁 / 重构方案 / 架构理解）+ 报告下载；
 * - failed：进度面板 + 失败 Alert；
 * - 404（含内存态任务表重启清空的场景）：警示说明；
 * - 预算熔断（W13-A4）：SSE 事件 / 详情 report 携带 budget_tripped 时，页面顶部
 *   渲染警示条（含已消耗 / 预算 tokens），并登记会话级缓存供仪表盘列表打标。
 */

import { DeleteOutlined } from "@ant-design/icons";
import { Alert, Button, Card, Flex, Popconfirm, Space, Spin, Tabs, Typography, message } from "antd";
import { useCallback, useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";

import { ApiError, deleteAudit, getAudit, getSummary, subscribeEvents } from "../../api/client";
import type { AuditDetail, AuditEvent, AuditSummary } from "../../api/client";
import { BudgetAlert } from "../../components/BudgetAlert";
import { StatusTag } from "../../components/StatusTag";
import {
  extractBudgetFromEvent,
  extractBudgetFromReport,
  mergeBudgetInfo,
  rememberDegradedAudit,
} from "../../utils/budget";
import type { BudgetTripInfo } from "../../utils/budget";
import { IssuesPanel } from "./IssuesPanel";
import { OverviewHead } from "./OverviewHead";
import { PatchesPanel } from "./PatchesPanel";
import { ProgressPanel } from "./ProgressPanel";
import { RefactorsPanel } from "./RefactorsPanel";
import { createStageMap, finishStages, applyStageEvent } from "./stages";
import type { StageMap } from "./stages";
import { UnderstandPanel } from "./UnderstandPanel";

const MONO_FONT = "Consolas, 'SFMono-Regular', Menlo, monospace";

type Phase = "loading" | "ready" | "not-found" | "error";

export default function TaskDetail() {
  const { id } = useParams<{ id: string }>();
  const auditId = id ?? "";
  const navigate = useNavigate();
  const [messageApi, contextHolder] = message.useMessage();

  const [phase, setPhase] = useState<Phase>("loading");
  const [loadError, setLoadError] = useState<string | null>(null);
  const [detail, setDetail] = useState<AuditDetail | null>(null);
  const [summary, setSummary] = useState<AuditSummary | null>(null);

  const [stageMap, setStageMap] = useState<StageMap>(createStageMap);
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [current, setCurrent] = useState<{ stage: string; message: string } | null>(null);
  // 预算熔断信息：SSE 熔断事件 / 详情 report 推导；null 表示未熔断或无数据（不显示警示）
  const [budget, setBudget] = useState<BudgetTripInfo | null>(null);

  /** 拉取任务详情；done 时附带拉摘要。终态时收尾阶段表（active/pending → done）。 */
  const load = useCallback(async () => {
    try {
      const d = await getAudit(auditId);
      setDetail(d);
      setPhase("ready");
      setLoadError(null);
      // 详情 report 防御性提取（当前契约不含预算字段，恒为 null；见 utils/budget.ts）
      const fromReport = extractBudgetFromReport(d.report);
      if (fromReport) setBudget((prev) => mergeBudgetInfo(prev, fromReport));
      if (d.status === "done") {
        setStageMap((prev) => finishStages(prev));
        setSummary(await getSummary(auditId));
      } else if (d.status === "failed") {
        setStageMap((prev) => finishStages(prev));
      }
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        setPhase("not-found");
      } else {
        setPhase("error");
        setLoadError(err instanceof ApiError ? err.detail : `加载任务失败：${String(err)}`);
      }
    }
  }, [auditId]);

  // 首次进入 / 切换任务：重置状态并加载
  useEffect(() => {
    setPhase("loading");
    setDetail(null);
    setSummary(null);
    setStageMap(createStageMap());
    setEvents([]);
    setCurrent(null);
    setBudget(null);
    if (!auditId) return;
    void load();
  }, [auditId, load]);

  // queued/running：订阅 SSE；终帧或断流降级结束后重新 load（状态已变化，订阅自动停）
  const active = detail !== null && (detail.status === "queued" || detail.status === "running");
  useEffect(() => {
    if (!active) return;
    const unsubscribe = subscribeEvents(
      auditId,
      (event) => {
        setEvents((prev) => [...prev, event]);
        // 熔断即时告警（stage:"init"）与 fix/testgen 跳过、done 进度帧均可能携带信号
        const sig = extractBudgetFromEvent(event);
        if (sig) setBudget((prev) => mergeBudgetInfo(prev, sig));
        if (event.stage) {
          setStageMap((prev) => applyStageEvent(prev, event));
          setCurrent({ stage: event.stage, message: event.message ?? "" });
        }
      },
      () => void load(),
      () => void load(),
    );
    return unsubscribe;
  }, [active, auditId, load]);

  // 已确认熔断：登记会话级缓存，供仪表盘列表对 done 行打「预算降级」标
  useEffect(() => {
    if (budget) rememberDegradedAudit(auditId, budget);
  }, [auditId, budget]);

  const onDelete = async () => {
    try {
      await deleteAudit(auditId);
      messageApi.success("任务已删除");
      navigate("/");
    } catch (err) {
      messageApi.error(err instanceof ApiError ? err.detail : "删除失败");
    }
  };

  if (phase === "loading") {
    return (
      <Card>
        <div style={{ textAlign: "center", padding: 48 }}>
          <Spin />
        </div>
      </Card>
    );
  }
  if (phase === "not-found") {
    return (
      <Alert
        type="warning"
        showIcon
        message="任务不存在"
        description="该任务可能已被删除，或服务重启过（任务列表为内存态，进程重启即清空）。可返回仪表盘重新发起审计。"
      />
    );
  }
  if (phase === "error" || detail === null) {
    return <Alert type="error" showIcon message="加载任务失败" description={loadError ?? "未知错误"} />;
  }

  const failed = detail.status === "failed";

  return (
    <Flex vertical gap={16}>
      {contextHolder}
      <Flex justify="space-between" align="center" wrap="wrap" gap={12}>
        <Space size={12} align="center" wrap>
          <Typography.Title level={4} style={{ margin: 0 }}>
            任务详情
          </Typography.Title>
          <Typography.Text code style={{ fontFamily: MONO_FONT, fontSize: 12.5 }}>
            {auditId}
          </Typography.Text>
          <StatusTag status={detail.status} />
        </Space>
        <Popconfirm
          title="删除该任务？"
          description="运行中的任务会先被取消；删除后不可恢复。"
          okText="确认删除"
          cancelText="取消"
          okButtonProps={{ danger: true }}
          onConfirm={() => void onDelete()}
        >
          <Button danger icon={<DeleteOutlined />}>
            删除任务
          </Button>
        </Popconfirm>
      </Flex>

      {/* 预算熔断警示条：标题下方、进度/健康分卡片上方（null 时不渲染） */}
      <BudgetAlert info={budget} />

      {(active || failed) && (
        <ProgressPanel
          stageMap={stageMap}
          current={current}
          events={events}
          failed={failed}
          errorMessage={detail.error ?? null}
        />
      )}

      {detail.status === "done" && summary !== null && (
        <>
          <Card title="审计概览">
            <OverviewHead summary={summary} />
          </Card>
          <Card styles={{ body: { paddingTop: 4 } }}>
            <Tabs
              defaultActiveKey="issues"
              items={[
                { key: "issues", label: "问题列表", children: <IssuesPanel auditId={auditId} /> },
                { key: "patches", label: "修复补丁", children: <PatchesPanel auditId={auditId} /> },
                { key: "refactors", label: "重构方案", children: <RefactorsPanel auditId={auditId} /> },
                { key: "understand", label: "架构理解", children: <UnderstandPanel auditId={auditId} /> },
              ]}
            />
          </Card>
        </>
      )}
    </Flex>
  );
}
