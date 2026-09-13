/**
 * diff 渲染：按演示页规则给行上色（add 绿 / del 红 / hunk 蓝 / hdr 加粗）。
 * 全部用 React 文本节点渲染，严禁 dangerouslySetInnerHTML。
 */

import type { CSSProperties } from "react";

import { classifyDiffLine, splitDiffLines } from "./diff";
import type { DiffLineKind } from "./diff";

const MONO_FONT = "Consolas, 'SFMono-Regular', Menlo, monospace";

const KIND_STYLE: Record<DiffLineKind, CSSProperties> = {
  add: { background: "#dcfce7", color: "#166534" },
  del: { background: "#fee2e2", color: "#991b1b" },
  hunk: { background: "#eff6ff", color: "#1d4ed8" },
  hdr: { fontWeight: 600, color: "#334155" },
  ctx: {},
};

export function DiffView({ diff }: { diff: string }) {
  if (!diff) {
    return (
      <div
        style={{
          fontFamily: MONO_FONT,
          fontSize: 12.5,
          padding: "8px 14px",
          color: "#64748b",
          background: "#f8fafc",
        }}
      >
        （无 diff 内容）
      </div>
    );
  }
  return (
    <div
      style={{
        fontFamily: MONO_FONT,
        fontSize: 12.5,
        lineHeight: 1.55,
        overflowX: "auto",
        background: "#fff",
      }}
    >
      {splitDiffLines(diff).map((line, idx) => (
        <div
          key={idx}
          style={{
            display: "block",
            whiteSpace: "pre",
            padding: "0 14px",
            ...KIND_STYLE[classifyDiffLine(line)],
          }}
        >
          {line.length > 0 ? line : " "}
        </div>
      ))}
    </div>
  );
}

export default DiffView;
