import { describe, expect, it } from "vitest";

import { applyStageEvent, createStageMap, deriveStageStatus, finishStages, stageStepStatus } from "./stages";

const ev = (stage: string, message: string, error?: string) => ({ stage, message, error });

describe("deriveStageStatus", () => {
  it("error 优先", () => {
    expect(deriveStageStatus(ev("detect", "检测完成", "boom"))).toBe("error");
  });
  it("跳过/skipped → skipped", () => {
    expect(deriveStageStatus(ev("fix", "跳过：离线模式"))).toBe("skipped");
    expect(deriveStageStatus(ev("fix", "skipped: no llm"))).toBe("skipped");
  });
  it("完成/就绪/已生成 → done", () => {
    expect(deriveStageStatus(ev("detect", "检测完成"))).toBe("done");
    expect(deriveStageStatus(ev("index", "符号表就绪"))).toBe("done");
    expect(deriveStageStatus(ev("report", "报告已生成"))).toBe("done");
  });
  it("其余 → active", () => {
    expect(deriveStageStatus(ev("detect", "正在扫描 12/34"))).toBe("active");
  });
});

describe("applyStageEvent", () => {
  it("前序 active 随后续阶段出现而传播为 done", () => {
    let map = createStageMap();
    map = applyStageEvent(map, ev("ingest", "接入中"));
    expect(map.ingest).toBe("active");
    map = applyStageEvent(map, ev("index", "索引中"));
    expect(map.ingest).toBe("done");
    expect(map.index).toBe("active");
  });
  it("error / skipped 状态不被后续事件覆盖", () => {
    let map = createStageMap();
    map = applyStageEvent(map, ev("fix", "修复失败", "boom"));
    expect(map.fix).toBe("error");
    map = applyStageEvent(map, ev("fix", "修复完成"));
    expect(map.fix).toBe("error");
    map = applyStageEvent(map, ev("testgen", "跳过：未启用"));
    expect(map.testgen).toBe("skipped");
  });
  it("未知阶段忽略", () => {
    const map = createStageMap();
    expect(applyStageEvent(map, ev("unknown", "x"))).toBe(map);
  });
});

describe("finishStages", () => {
  it("active/pending → done，error/skipped 保留", () => {
    let map = createStageMap();
    map = applyStageEvent(map, ev("ingest", "完成"));
    map = applyStageEvent(map, ev("index", "完成"));
    map = applyStageEvent(map, ev("detect", "进行中"));
    map = applyStageEvent(map, ev("fix", "跳过"));
    map = applyStageEvent(map, ev("testgen", "炸了", "x"));
    const done = finishStages(map);
    expect(done.ingest).toBe("done");
    expect(done.index).toBe("done");
    expect(done.detect).toBe("done"); // active 收尾
    expect(done.fix).toBe("skipped"); // 保留
    expect(done.testgen).toBe("error"); // 保留
    expect(done.report).toBe("done"); // pending 收尾
  });
});

describe("stageStepStatus", () => {
  it("映射 antd Steps 状态，skipped 显示为 finish", () => {
    expect(stageStepStatus("pending")).toBe("wait");
    expect(stageStepStatus("active")).toBe("process");
    expect(stageStepStatus("done")).toBe("finish");
    expect(stageStepStatus("skipped")).toBe("finish");
    expect(stageStepStatus("error")).toBe("error");
  });
});
