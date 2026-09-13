import { describe, expect, it } from "vitest";

import { classifyDiffLine, splitDiffLines } from "./diff";

describe("classifyDiffLine", () => {
  it("文件头优先于增删行判定（+++ / ---）", () => {
    expect(classifyDiffLine("+++ b/app/main.py")).toBe("hdr");
    expect(classifyDiffLine("--- a/app/main.py")).toBe("hdr");
  });
  it("@@ → hunk，+/- → add/del，其余 → ctx", () => {
    expect(classifyDiffLine("@@ -1,3 +1,4 @@")).toBe("hunk");
    expect(classifyDiffLine("+new_code()")).toBe("add");
    expect(classifyDiffLine("-old_code()")).toBe("del");
    expect(classifyDiffLine(" context")).toBe("ctx");
    expect(classifyDiffLine("")).toBe("ctx");
  });
});

describe("splitDiffLines", () => {
  it("CRLF 归一为 LF 后拆行", () => {
    expect(splitDiffLines("a\r\nb\nc")).toEqual(["a", "b", "c"]);
  });
});
