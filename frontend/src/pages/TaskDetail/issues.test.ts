import { describe, expect, it } from "vitest";

import type { IssueItem } from "../../api/types";
import { filterIssuesByKeyword, fmtConfidence, fmtLocation, sortIssues } from "./issues";

const issue = (over: Partial<IssueItem>): IssueItem => ({
  id: "PY-001",
  severity: "high",
  category: "bug",
  file: "app/main.py",
  line_start: 10,
  line_end: 12,
  title: "SQL 拼接注入",
  description: "存在注入",
  suggestion: "使用参数化查询",
  evidence: [],
  code_snippet: "",
  confidence: 0.9,
  source: "rule",
  fix_status: "none",
  patch_id: "",
  ...over,
});

describe("filterIssuesByKeyword", () => {
  const issues = [
    issue({ id: "PY-001", title: "SQL 注入", file: "db/dao.py", description: "拼接", suggestion: "参数化" }),
    issue({ id: "JS-002", title: "裸 except", file: "app/util.py", description: "捕获所有异常", suggestion: "缩小范围" }),
  ];

  it("空关键词返回全部", () => {
    expect(filterIssuesByKeyword(issues, "")).toHaveLength(2);
    expect(filterIssuesByKeyword(issues, "  ")).toHaveLength(2);
  });
  it("匹配 id / 标题 / 文件 / 描述 / 修复建议，大小写不敏感", () => {
    expect(filterIssuesByKeyword(issues, "js-0")).toHaveLength(1);
    expect(filterIssuesByKeyword(issues, "注入")).toHaveLength(1);
    expect(filterIssuesByKeyword(issues, "UTIL.PY")).toHaveLength(1);
    expect(filterIssuesByKeyword(issues, "参数化")).toHaveLength(1);
    expect(filterIssuesByKeyword(issues, "拼接")).toHaveLength(1);
  });
  it("无命中返回空数组", () => {
    expect(filterIssuesByKeyword(issues, "不存在")).toHaveLength(0);
  });
});

describe("sortIssues", () => {
  it("严重度 rank → 文件名 → 起始行", () => {
    const sorted = sortIssues([
      issue({ id: "a", severity: "low", file: "b.py", line_start: 2 }),
      issue({ id: "b", severity: "critical", file: "z.py", line_start: 1 }),
      issue({ id: "c", severity: "high", file: "a.py", line_start: 9 }),
      issue({ id: "d", severity: "high", file: "a.py", line_start: 3 }),
    ]);
    expect(sorted.map((i) => i.id)).toEqual(["b", "d", "c", "a"]);
  });
});

describe("fmtLocation / fmtConfidence", () => {
  it("位置：单行与区间", () => {
    expect(fmtLocation(issue({ line_end: 10 }))).toBe("app/main.py:10");
    expect(fmtLocation(issue({ line_end: 15 }))).toBe("app/main.py:10-15");
  });
  it("置信度 0~1 → 整数百分比", () => {
    expect(fmtConfidence(0.9)).toBe("90%");
    expect(fmtConfidence(0.856)).toBe("86%");
    expect(fmtConfidence(Number.NaN)).toBe("0%");
  });
});
