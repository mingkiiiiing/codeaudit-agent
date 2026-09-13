import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, createAudit, listAudits, subscribeEvents, uploadAuditZip } from "./client";

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

// ---------------------------------------------------------------- REST

describe("client REST", () => {
  it("listAudits 拼接 limit/offset 查询参数并解析响应", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        total: 2,
        audits: [
          { audit_id: "aaaa1111", status: "done", created_at: "2026-09-13T10:00:00" },
          { audit_id: "bbbb2222", status: "running" },
        ],
      }),
    );

    const data = await listAudits(10, 0);

    expect(fetchMock).toHaveBeenCalledWith("/api/audits?limit=10&offset=0", undefined);
    expect(data.total).toBe(2);
    expect(data.audits).toHaveLength(2);
    expect(data.audits[0].audit_id).toBe("aaaa1111");
  });

  it("listAudits 无参数时请求裸路径", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ total: 0, audits: [] }));
    const data = await listAudits();
    expect(fetchMock).toHaveBeenCalledWith("/api/audits", undefined);
    expect(data.audits).toEqual([]);
  });

  it("createAudit 以 JSON 发送请求体", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ audit_id: "newid" }));

    const body = { source_path: "D:\\demo\\my-project", do_fix: true, do_tests: false };
    const result = await createAudit(body);

    expect(result).toEqual({ audit_id: "newid" });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/audits");
    expect(init?.method).toBe("POST");
    expect(init?.headers).toEqual({ "Content-Type": "application/json" });
    expect(JSON.parse(init?.body as string)).toEqual(body);
  });

  it("uploadAuditZip 使用 FormData 上传（布尔转字符串）", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ audit_id: "zipid" }));
    const file = new File(["PK"], "project.zip", { type: "application/zip" });

    const result = await uploadAuditZip(file, { do_fix: true, do_tests: true });

    expect(result).toEqual({ audit_id: "zipid" });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/audits/upload");
    expect(init?.method).toBe("POST");
    const form = init?.body as FormData;
    expect(form).toBeInstanceOf(FormData);
    expect(form.get("file")).toBe(file);
    expect(form.get("do_fix")).toBe("true");
    expect(form.get("do_tests")).toBe("true");
  });

  it("非 2xx 时解析 detail 字段并抛 ApiError(detail, status)", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: "源路径不存在：D:\\x" }, 400));

    await expect(createAudit({ source_path: "D:\\x", do_fix: false, do_tests: false })).rejects.toMatchObject({
      name: "ApiError",
      detail: "源路径不存在：D:\\x",
      status: 400,
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("deleteAudit 204 时返回 undefined 而不解析 JSON", async () => {
    fetchMock.mockResolvedValueOnce({ ok: true, status: 204 } as unknown as Response);
    await expect(import("./client").then((m) => m.deleteAudit("abc"))).resolves.toBeUndefined();
    expect(fetchMock).toHaveBeenCalledWith("/api/audits/abc", { method: "DELETE" });
  });
});

// ---------------------------------------------------------------- SSE

type MessageEventLike = { data: string };

class FakeEventSource {
  static instances: FakeEventSource[] = [];

  url: string;
  closed = false;
  onmessage: ((ev: MessageEventLike) => void) | null = null;
  onerror: ((ev: unknown) => void) | null = null;

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  close(): void {
    this.closed = true;
  }

  emit(json: string): void {
    this.onmessage?.({ data: json });
  }

  fail(): void {
    this.onerror?.(new Error("connection lost"));
  }
}

describe("subscribeEvents", () => {
  beforeEach(() => {
    FakeEventSource.instances = [];
    vi.stubGlobal("EventSource", FakeEventSource as unknown as typeof EventSource);
  });

  it("收到事件回调 onEvent，收到 done 帧关闭连接并回调 onDone", () => {
    const onEvent = vi.fn();
    const onDone = vi.fn();
    const onError = vi.fn();

    const cleanup = subscribeEvents("a1", onEvent, onDone, onError);

    const es = FakeEventSource.instances[0];
    expect(es.url).toBe("/api/audits/a1/events");

    es.emit(JSON.stringify({ stage: "ingest", message: "开始接入" }));
    expect(onEvent).toHaveBeenCalledWith({ stage: "ingest", message: "开始接入" });
    expect(onDone).not.toHaveBeenCalled();

    es.emit(JSON.stringify({ type: "done" }));
    expect(onDone).toHaveBeenCalledTimes(1);
    expect(es.closed).toBe(true);
    expect(onError).not.toHaveBeenCalled();

    cleanup(); // 幂等清理
    expect(es.closed).toBe(true);
  });

  it("非 JSON 帧被忽略且不中断订阅", () => {
    const onEvent = vi.fn();
    const cleanup = subscribeEvents("a2", onEvent, vi.fn(), vi.fn());
    const es = FakeEventSource.instances[0];

    expect(() => es.emit("not-json")).not.toThrow();
    expect(onEvent).not.toHaveBeenCalled();

    cleanup();
  });

  it("onerror 后降级为 1s 轮询：running → done 时回调 onDone 并停止轮询", async () => {
    vi.useFakeTimers();
    fetchMock
      .mockResolvedValueOnce(
        jsonResponse({ audit_id: "a3", status: "running", error: null }),
      )
      .mockResolvedValueOnce(jsonResponse({ audit_id: "a3", status: "done", error: null }));

    const onDone = vi.fn();
    const onError = vi.fn();
    const cleanup = subscribeEvents("a3", vi.fn(), onDone, onError);

    const es = FakeEventSource.instances[0];
    es.fail(); // 触发降级
    expect(es.closed).toBe(true);

    await vi.advanceTimersByTimeAsync(1000);
    expect(onDone).not.toHaveBeenCalled(); // 第一轮：running

    await vi.advanceTimersByTimeAsync(1000);
    expect(onDone).toHaveBeenCalledTimes(1); // 第二轮：done

    await vi.advanceTimersByTimeAsync(10_000);
    expect(onDone).toHaveBeenCalledTimes(1); // interval 已清除
    expect(fetchMock).toHaveBeenCalledTimes(2);

    cleanup();
  });

  it("轮询遇 404 时回调 onError 并停止轮询", async () => {
    vi.useFakeTimers();
    fetchMock.mockResolvedValue(jsonResponse({ detail: "任务不存在：a4" }, 404));

    const onDone = vi.fn();
    const onError = vi.fn();
    const cleanup = subscribeEvents("a4", vi.fn(), onDone, onError);

    FakeEventSource.instances[0].fail();
    await vi.advanceTimersByTimeAsync(1000);

    expect(onError).toHaveBeenCalledTimes(1);
    const err = onError.mock.calls[0][0] as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(404);
    expect(err.detail).toBe("任务不存在：a4");
    expect(onDone).not.toHaveBeenCalled();
    expect(fetchMock).toHaveBeenCalledTimes(1); // 停止轮询

    cleanup();
  });

  it("清理函数同时关闭 EventSource 与轮询 interval", async () => {
    vi.useFakeTimers();
    fetchMock.mockResolvedValue(jsonResponse({ audit_id: "a5", status: "running", error: null }));

    const onDone = vi.fn();
    const cleanup = subscribeEvents("a5", vi.fn(), onDone, vi.fn());

    FakeEventSource.instances[0].fail(); // 降级轮询
    cleanup();
    await vi.advanceTimersByTimeAsync(10_000);

    expect(fetchMock).not.toHaveBeenCalled();
    expect(onDone).not.toHaveBeenCalled();
  });
});
