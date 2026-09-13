import { Badge, Tag } from "antd";

import { STATUS_LABEL } from "../utils/format";

/**
 * 任务状态标签：done/failed 用 Tag（绿/红），queued/running 用 Badge processing + 文案。
 */
export function StatusTag({ status }: { status: string }) {
  if (status === "done") {
    return <Tag color="green">{STATUS_LABEL.done}</Tag>;
  }
  if (status === "failed") {
    return <Tag color="red">{STATUS_LABEL.failed}</Tag>;
  }
  const label = STATUS_LABEL[status] ?? status;
  if (status === "queued" || status === "running") {
    return <Badge status="processing" text={label} />;
  }
  return <Tag>{label}</Tag>;
}

export default StatusTag;
