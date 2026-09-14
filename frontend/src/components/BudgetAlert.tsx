/**
 * 预算熔断降级警示条（W13-A4）：TaskDetail 页面顶部渲染。
 * info 为 null（未熔断 / 旧任务无该数据）时不渲染——诚实降级，不伪造。
 */

import { Alert } from "antd";

import type { BudgetTripInfo } from "../utils/budget";
import { formatBudgetMessage } from "../utils/budget";

export function BudgetAlert({ info }: { info: BudgetTripInfo | null }) {
  if (!info) return null;
  return (
    <Alert
      type="warning"
      showIcon
      message={formatBudgetMessage(info)}
      description="预算耗尽后新的 LLM 调用将被拒绝，后续阶段的产出可能不完整，请结合规则引擎结果评估本次报告。"
    />
  );
}

export default BudgetAlert;
