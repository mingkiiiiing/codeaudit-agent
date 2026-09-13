/**
 * 架构理解 Tab：getUnderstand 拉取 report.architecture（结构宽松的 ArchitectureCard）。
 * 防御性通用渲染：原始值直接展示、数组列表化、对象键值展开，最大深度 3 层，
 * 超深 / 超长内容折叠省略；architecture 为 null 时诚实展示空态。
 */

import { useCallback, useEffect, useState } from "react";
import { Alert, Card, Empty, Spin, Typography } from "antd";

import { ApiError, getUnderstand } from "../../api/client";
import type { ArchitectureCard } from "../../api/client";

const MAX_DEPTH = 3;
const TEXT_MAX = 200;

function truncate(text: string): string {
  return text.length > TEXT_MAX ? `${text.slice(0, TEXT_MAX)}…` : text;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** 通用防御渲染：深度超过 MAX_DEPTH 时以省略号收尾（信息降级但不崩）。 */
function renderValue(value: unknown, depth: number, keyPrefix: string): React.ReactNode {
  if (value === null || value === undefined) {
    return <Typography.Text type="secondary">-</Typography.Text>;
  }
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return <span>{truncate(String(value))}</span>;
  }
  if (depth >= MAX_DEPTH) {
    return (
      <Typography.Text type="secondary" code style={{ fontSize: 12 }}>
        {truncate(JSON.stringify(value)) ?? "…"}
      </Typography.Text>
    );
  }
  if (Array.isArray(value)) {
    if (value.length === 0) {
      return <Typography.Text type="secondary">（空）</Typography.Text>;
    }
    return (
      <ul style={{ margin: "2px 0", paddingLeft: 20, lineHeight: 1.6 }}>
        {value.slice(0, 20).map((item, idx) => (
          <li key={`${keyPrefix}[${idx}]`}>{renderValue(item, depth + 1, `${keyPrefix}[${idx}]`)}</li>
        ))}
        {value.length > 20 && (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            …共 {value.length} 项
          </Typography.Text>
        )}
      </ul>
    );
  }
  if (isPlainObject(value)) {
    const entries = Object.entries(value);
    if (entries.length === 0) {
      return <Typography.Text type="secondary">（空）</Typography.Text>;
    }
    return (
      <div style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: "4px 14px" }}>
        {entries.map(([key, val]) => (
          <RenderedEntry key={`${keyPrefix}.${key}`} label={key} depth={depth} value={val} />
        ))}
      </div>
    );
  }
  return <span>{truncate(String(value))}</span>;
}

function RenderedEntry({ label, value, depth }: { label: string; value: unknown; depth: number }) {
  const scalar =
    value === null || ["string", "number", "boolean"].includes(typeof value);
  return (
    <>
      <Typography.Text
        type="secondary"
        style={{ fontSize: 12.5, lineHeight: 1.6, whiteSpace: "nowrap" }}
      >
        {label}
      </Typography.Text>
      <div style={{ fontSize: 12.5, lineHeight: 1.6, minWidth: 0, wordBreak: "break-all" }}>
        {scalar || isPlainObject(value) || Array.isArray(value)
          ? renderValue(value, depth + 1, label)
          : String(value)}
      </div>
    </>
  );
}

export function UnderstandPanel({ auditId }: { auditId: string }) {
  const [architecture, setArchitecture] = useState<ArchitectureCard | null | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);

  const fetchUnderstand = useCallback(async () => {
    try {
      const data = await getUnderstand(auditId);
      setArchitecture(data.architecture);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : `加载架构理解失败：${String(err)}`);
    }
  }, [auditId]);

  useEffect(() => {
    void fetchUnderstand();
  }, [fetchUnderstand]);

  if (error) {
    return <Alert type="error" showIcon message="加载架构理解失败" description={error} />;
  }
  if (architecture === undefined) {
    return (
      <div style={{ textAlign: "center", padding: 32 }}>
        <Spin />
      </div>
    );
  }
  if (architecture === null) {
    return <Empty description="该任务未生成架构卡片（理解阶段被跳过或失败时为空）" />;
  }

  return (
    <Card size="small" title="架构卡片（understand 阶段产物）">
      {renderValue(architecture, 0, "root")}
    </Card>
  );
}

export default UnderstandPanel;
