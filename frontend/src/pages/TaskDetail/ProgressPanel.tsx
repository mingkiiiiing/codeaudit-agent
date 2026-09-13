/**
 * 运行中/失败的任务进度面板：七阶段 Steps + 当前事件行 + 折叠事件日志（深色 mono）+ 失败 Alert。
 * 阶段状态推导与演示页一致（见 stages.ts）。
 */

import { MinusOutlined } from "@ant-design/icons";
import { Alert, Card, Collapse, Steps, Typography } from "antd";
import { useEffect, useRef } from "react";

import type { AuditEvent } from "../../api/types";
import { STAGE_DEFS, stageStepStatus } from "./stages";
import type { StageMap } from "./stages";

const MONO_FONT = "Consolas, 'SFMono-Regular', Menlo, monospace";
const SKIPPED_GREY = "#94a3b8";

export interface ProgressPanelProps {
  stageMap: StageMap;
  /** 当前事件（最近一条带 stage 的事件）。 */
  current: { stage: string; message: string } | null;
  events: AuditEvent[];
  /** 任务是否已落失败终态。 */
  failed: boolean;
  /** 失败原因（audit.error）。 */
  errorMessage: string | null;
}

export function ProgressPanel({ stageMap, current, events, failed, errorMessage }: ProgressPanelProps) {
  const logRef = useRef<HTMLDivElement | null>(null);

  // 新事件到达时滚动到底部（日志区展开挂载后生效）
  useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [events]);

  const stepItems = STAGE_DEFS.map((def) => {
    const status = stageMap[def.key] ?? "pending";
    const skipped = status === "skipped";
    return {
      key: def.key,
      title: def.label,
      status: stageStepStatus(status),
      description: skipped ? (
        <span style={{ color: SKIPPED_GREY }}>已跳过</span>
      ) : status === "active" ? (
        <span style={{ color: "#0f766e" }}>进行中</span>
      ) : undefined,
      ...(skipped ? { icon: <MinusOutlined style={{ color: SKIPPED_GREY, fontSize: 12 }} /> } : {}),
    };
  });

  return (
    <Card title="任务进度">
      <Steps size="small" items={stepItems} />

      <div
        style={{
          fontFamily: MONO_FONT,
          fontSize: 12.5,
          color: "#1e293b",
          background: "#f8fafc",
          border: "1px dashed #e2e8f0",
          borderRadius: 8,
          padding: "8px 12px",
          minHeight: 34,
          marginTop: 16,
          wordBreak: "break-all",
        }}
      >
        {current ? (
          <>
            <span style={{ color: "#0f766e", fontWeight: 600, marginRight: 6 }}>
              [{current.stage}]
            </span>
            {current.message}
          </>
        ) : (
          "等待事件 …"
        )}
      </div>

      {failed && (
        <Alert
          type="error"
          showIcon
          style={{ marginTop: 12 }}
          message="任务失败"
          description={errorMessage || "未知错误"}
        />
      )}

      <Collapse
        ghost
        size="small"
        style={{ marginTop: 8 }}
        items={[
          {
            key: "log",
            label: `事件日志（${events.length} 条）`,
            children: (
              <div
                ref={logRef}
                style={{
                  maxHeight: 200,
                  overflowY: "auto",
                  background: "#0f172a",
                  color: "#e2e8f0",
                  borderRadius: 8,
                  padding: "10px 12px",
                  fontFamily: MONO_FONT,
                  fontSize: 12,
                }}
              >
                {events.map((ev, idx) => (
                  <div key={idx} style={{ padding: "1px 0" }}>
                    <span style={{ color: "#5eead4", fontWeight: 600 }}>[{ev.stage}]</span>{" "}
                    <Typography.Text style={{ color: "#e2e8f0", fontFamily: MONO_FONT, fontSize: 12 }}>
                      {ev.message ?? ""}
                    </Typography.Text>
                  </div>
                ))}
              </div>
            ),
          },
        ]}
      />
    </Card>
  );
}

export default ProgressPanel;
