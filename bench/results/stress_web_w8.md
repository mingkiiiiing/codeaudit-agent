# Web 服务压力测试报告（bench/stress/run_stress_web.py，W8-A4）

## 环境

- **生成时间**：2026-09-13 08:33:33
- **主机 / 平台**：DESKTOP-KS9DBJD / Windows 11 (AMD64)
- **CPU**：AMD64 Family 25 Model 117 Stepping 2, AuthenticAMD
- **Python**：3.13.9
- **被测服务**：server/app.py（FastAPI + uvicorn，**单进程单 worker**），base=http://127.0.0.1:8910
- **网络**：全程离线（FakeLLM / 纯规则模式，未配置也不读取 GLM_API_KEY）
- **口径**：真实 HTTP 客户端计时（httpx/asyncio）；任务表为内存态；分位数为客户端口径

## 摘要

| 场景 | 关键指标 | 状态 |
|---|---|---|
| S1 读端点突发 | 32 并发 × 560 请求，RPS=304.4，p95=123.7ms，错误=0 | OK |
| S2 并发审计 | 6 任务，终态=done,done,done,done,done,done，ID 互异=True，端到端 p50=14.58s / max=14.59s，吞吐=24.7 任务/min | OK |
| S3 SSE 并发流 | 24 连接，收到 done 终帧=24，首事件 p50=102.3ms，收流 p95=133.0ms | OK |
| S4 zip 上传 | 12 次（zip 2.6KB），p50=379.5ms / p95=407.1ms，非 zip 负样本 HTTP 400 | OK |

## 场景明细

### S1 读端点突发

| 指标 | 值 |
|---|---|
| concurrency | `32` |
| total | `560` |
| rps | `304.4` |
| wall | `1.84` |
| p50 | `102.9` |
| p95 | `123.7` |
| p99 | `132.7` |
| mean | `103.4` |
| errors | `0` |
| error_samples | `[]` |

### S2 并发审计

| 指标 | 值 |
|---|---|
| concurrency | `6` |
| statuses | `['done', 'done', 'done', 'done', 'done', 'done']` |
| distinct_ids | `True` |
| e2e_sec | `[14.59, 14.58, 14.57, 14.58, 14.58, 14.58]` |
| e2e_p50 | `14.58` |
| e2e_max | `14.59` |
| wall | `14.59` |
| throughput_per_min | `24.7` |
| list_total_observed | `7` |

### S3 SSE 并发流

| 指标 | 值 |
|---|---|
| streams | `24` |
| saw_event | `576` |
| saw_done | `24` |
| task_status_at_end | `done` |
| wall | `0.14` |
| first_event_p50_ms | `102.3` |
| first_event_p95_ms | `132.1` |
| drain_p95_ms | `133.0` |

### S4 zip 上传

| 指标 | 值 |
|---|---|
| rounds | `12` |
| zip_kb | `2.6` |
| latencies_ms | `[379.4536000000335, 377.35049999992043, 369.9479999995674, 370.58699999988676, 380.35569999965446, 407.1146999999655, 380.0083999999515, 382.64729999991687, 407.1336000001793, 370.8699999997407, 368.65560000023834, 371.27879999979996]` |
| negative_status | `400` |

## 诚实边界

- 单 worker 内存态任务表：本报告只对 0.5.0 的真实形态负责，多 worker/持久化属后续 Wave。
- S2 的 C 个任务共享同一 uvicorn 事件循环，规则扫描为 CPU 密集，延迟随并发近线性增长属预期。
- S3 依赖服务端 SSE 历史回放语义（事件列表重放后跟随新事件），连接慢于任务结束仍可收齐。
- S1 的目标报告为 files=80 合成项目，payload 属轻中量；更大报告的分位数值会相应上移。
