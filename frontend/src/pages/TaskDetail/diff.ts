/**
 * diff 行分类规则：沿用 web/index.html 演示页 renderDiff 的判定顺序 ——
 * 先判 +++/--- 文件头，再判 @@ hunk 头，再判 +/- 增删行，其余为上下文行。
 */

export type DiffLineKind = "add" | "del" | "hunk" | "hdr" | "ctx";

export function classifyDiffLine(line: string): DiffLineKind {
  if (line.startsWith("+++") || line.startsWith("---")) return "hdr";
  if (line.startsWith("@@")) return "hunk";
  if (line.startsWith("+")) return "add";
  if (line.startsWith("-")) return "del";
  return "ctx";
}

/** 统一 CRLF → LF 后按行拆分（与演示页一致）。 */
export function splitDiffLines(diff: string): string[] {
  return diff.replace(/\r\n/g, "\n").split("\n");
}
