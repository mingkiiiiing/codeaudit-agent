import { Tag } from "antd";

import { SEV_LABEL, sevColor } from "../utils/format";

/** 严重度标签：品牌语义色（critical 红 / high 橙 / medium 黄 / low 蓝）+ 中文文案。 */
export function SevTag({ severity }: { severity: string }) {
  const label = SEV_LABEL[severity] ?? severity;
  return <Tag color={sevColor(severity)}>{label}</Tag>;
}

export default SevTag;
