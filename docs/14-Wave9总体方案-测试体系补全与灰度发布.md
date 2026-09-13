# Wave 9 总体方案——测试体系补全（压力 / 并发 / 算法 / 恶意 / 灰度）与后续路线

> 2026-09-13 ｜ 状态：已执行 ｜ 关联：docs/10（质量攻坚）、docs/12（合规与提速）、docs/13（Web 前后端）
>
> 本轮目标：对 0.5.0 做一次全面审查，补齐五类测试资产（压力 / 并发 / 算法 / 恶意 / 灰度），
> 登记 adversarial 审查发现，并给出灰度发布方案与 Wave 9+ 路线。

## 1. 审查结论摘要

代码审查覆盖 server/app.py（HTTP 面）、audit/ingest（zip 接入）、audit/sandbox（子进程沙箱）、
audit/orchestrator（流水线）与既有测试 / 压测资产。总体判断：**架构分层清晰、防御性编程
到位**（zip 炸弹声明量前置校验、路径逃逸成员跳过、任务表 FIFO 容量淘汰、弱引用任务集、
BaseException 兜底终态、白名单测试框架、输出截尾），1060 项既有 pytest 全绿。本轮新发现
5 项风险（F1–F5），**无一是崩溃级缺陷**，全部属于"本地单机工具形态下的已知设计边界"，
但都会在多人 / 公网部署形态下升级为真实攻击面，登记如下：

| 编号 | 发现 | 位置 | 危害形态 | 状态 |
|---|---|---|---|---|
| F1 | REST API 无鉴权，`source_path` 可指向任意本地目录，报告回传源码片段 | `server/app.py` create_audit | 未授权读取任意本地代码（含密钥注释） | 取证确认（A2），本地工具设计如此 |
| F2 | 上传先 `await file.read()` 全量入内存再校验 200MB 上限 | `server/app.py` upload_audit | 并发大上传 → 服务端内存抬升 / OOM | 取证确认（A6，RSS 峰值同量级） |
| F3 | Windows 沙箱无文件系统隔离，子进程可写 cwd 之外 | `audit/sandbox/executor.py` | 恶意被审项目的"现有测试"可写任意路径 | 取证确认（A7）；文档已声明，生产建议 Docker |
| F4 | 沙箱输出经 PIPE 全量缓冲后才截尾（80 行） | `audit/sandbox/executor.py` _drain | GB 级 stdout → 瞬时内存峰值 | 取证确认（A7，200MB 输出炸弹） |
| F5 | 无任务准入控制：建任务速率、并发 running 数均无上限；10 路并发审计期间服务端事件循环被 CPU 密集任务饿死（23s 内 health 仅响应 2 次，即 CPU 耗尽实证） | `server/app.py` _start_audit | 任务洪泛 → CPU/磁盘耗尽、健康检查失联 | 取证确认（A3：10 路全接纳，10×200 无一拒绝） |

既有防御（本轮对抗测试实证有效）：zip 路径逃逸成员（`../`、绝对路径、盘符）双防线
（自研跳过 + CPython 清洗）、1GB 声明量解压炸弹拒绝、坏 zip/空 zip 不崩、奇葩文件名
（Unicode/emoji/保留名/结尾点）不崩、Windows 保留名任务落 failed 终态、参数滥用矩阵
（非法分页/过滤/格式/ID 注入）全部精确 4xx、create-delete 抖动竞态无 5xx、100 路 SSE
中途 DELETE 全部收流不悬挂。

## 2. 测试矩阵全景（五类 × 资产清单）

| 类别 | 已有资产（0.5.0 前） | 本轮新增（W9） | 规划（W10+） |
|---|---|---|---|
| **算法正确性** | bench/ goldset + mini_bench（P/R 对拍 GOLDEN_ISSUES）；test_sarif_semantics | `tests/property/test_algorithm_invariants.py`：T1 确定性（同输入两次问题清单逐字段一致）/ T2 健康分单调性（追加 critical 不升分，50 组随机性质）/ T3 干净语料零 critical/high 误报 / T4 SARIF 结构不变量 / T5 summary 与 issues 计数自洽 / T6 过滤与分页不变量（端到端） | 在线 GLM 通道注入鲁棒性评估（等 key）；规则 FPR 分语料统计；大库上 goldset 扩容 |
| **并发** | test_api_concurrency（3 路无串扰、超限不崩、LRU 淘汰）；run_stress 场景 5；run_stress_web S2/S3 | A4 create→立即 DELETE 抖动竞态 30 轮 + 幽灵 DELETE；A5 100 路 SSE + 运行中 DELETE 悬挂检测 | 多 worker（uvicorn workers>1）形态重跑；持久化任务表后重跑；同一 SQLite 索引并发读写边界 |
| **压力** | run_stress 六场景（ingest+index / 规则吞吐 / LLM 并发扩展 / 预算熔断 / server 并发 / R1-8 N+1）；run_stress_web 四场景（读突发 304 RPS / 6 并发审计 / 24 路 SSE / 上传 12 次） | A3 洪泛 60 任务 + 10 路并发真实任务；A6 210MB 不可压缩上传（413 + RSS 取证）、60MB 高压缩比 ×3 | S9 soak 持续混合负载 + RSS 曲线（泄漏判定）；S10 深队列 20 任务公平性；S11 多 worker 吞吐对比 |
| **恶意性** | 单测层：坏 zip / 炸弹守卫 / 忽略目录 / 规模守卫 / 失败清理 | `bench/adversarial/run_adversarial.py` 八场景（A1 军火库 / A2 参数滥用+越权取证 / A3 洪泛 / A4 抖动 / A5 SSE / A6 超大上传 / A7 沙箱逃逸 / A8 提示注入离线实证） | zip 本地头伪造等结构层深攻；A8 在线版（真实 GLM 下注入对 Review/Verify Agent 的影响）；Docker 沙箱形态回归 |
| **灰度** | （无） | `tests/integration/test_gray_release.py`：C1 旧版报告契约向后兼容（v0.3 形态 dict 在当前代码可反序列化 + 三格式渲染 + 往返不漂移）/ C2 特性开关 A/B 等价（rule_scan_workers 1 vs 4、硬链接 vs 复制，语义快照相等）/ C3 API 路由面冻结（openapi paths 与契约 v2 清单精确相等） | L2 金丝雀双版本回放（§4）；报告 schema_version 演进策略固化 |

**CI 接入口径**：`tests/property`、`tests/integration/test_gray_release.py` 随既有 pytest 矩阵
自动执行（新增 11 用例，约 5s）；`bench/adversarial` 为重型工具（真实子进程 + 210MB 素材），
与既有 bench/stress 系列同口径——本地 / 手动跑，报告归档 `bench/results/`，不进 CI。

## 3. 压测设计（口径约定）

沿用 W5/W8 两代压测的既定口径，W9 补齐对抗维度后完整分层如下：

- **管线级**（run_stress.py）：合成项目 200/2000 文件档，指标 = 各阶段耗时、KLOC 吞吐、
  内存峰值（tracemalloc）、规则命中集合指纹（并行等价性校验）。
- **服务级**（run_stress_web.py）：真实 HTTP（httpx 异步 + uvicorn 单 worker），指标 =
  RPS / p50/p95/p99（客户端口径）/ 错误数 / 任务端到端延迟 / SSE 首帧与收流。
- **对抗级**（run_adversarial.py，W9）：不追吞吐，追**失效模式**——每个场景的通过判据是
  "服务存活 + 任务落终态 + 无 5xx + 无越界落地文件 + marker 全树扫描无逃逸"，另带
  取证类指标（RSS 峰值、并发峰值 active、密文是否回传）。
- **诚实边界**：所有压测均离线（FakeLLM / 纯规则）；单 worker 内存态任务表；多 worker /
  持久化后的数字不引用本轮报告。

## 4. 灰度发布方案（四层防线）

本项目形态是"本地工具 + 可选自托管服务"，灰度按四层设计，前三层已有自动化落点：

- **L0 契约冻结（CI 绊网，已落地）**：API 路由面冻结（C3：openapi paths 与冻结清单精确
  相等，多删都红）；报告 `schema_version` 字段 + 旧版报告向后兼容渲染（C1）；SARIF 2.1.0
  语义测试（既有）。任何契约变更必须显式改清单 / 升版本号，CI 拦截意外漂移。
- **L1 双路对拍（开关等价，已落地）**：实验开关（rule_scan_workers、link_same_volume）
  切换不改语义（C2 语义快照相等）。灰度放量期间新旧代码路径并行跑同一请求集，结果
  diff 为空是放行前置条件。
- **L2 金丝雀双版本回放（规划 W10）**：同机起 vN-1 与 vN 两端口（git worktree 取旧 tag），
  用固定请求集（读端点 + mini_app 审计 + zip 上传）双发，diff 响应体（剥离时间戳 / audit_id /
  duration 类字段）。差异非空 → 阻断发布。脚本化 `bench/canary/replay.py`，判据复用 L1 的
  归一化函数。
- **L3 用户侧灰度（能力已具备）**：`--diff` 增量审计 + 基线抑制（"存量豁免、增量把关"）、
  `--check --fail-on` PR 门禁、报告三格式向后兼容。这是产品语义上的灰度：新版本只对
  增量代码生效，存量告警按基线豁免。
- **回滚判据**（自托管形态）：健康分漂移 > 5 分（同一代码库两版本对拍）、SSE 收流率
  < 100%、5xx 率 > 0、任务终态落 failed 比例 > 基线 +2pp，任一命中即回滚。

## 5. Wave 9 交付物清单

| 交付物 | 路径 | 规模 | 状态 |
|---|---|---|---|
| 对抗与滥用测试运行器 | `bench/adversarial/run_adversarial.py` | 八场景，真实 HTTP + 库层 | ✅ 全绿（报告见 bench/results/adversarial_w9.md） |
| 算法不变量测试 | `tests/property/test_algorithm_invariants.py` | 6 组性质用例 | ✅ 全绿（进 CI） |
| 灰度保障测试 | `tests/integration/test_gray_release.py` | 3 组防线用例 | ✅ 全绿（进 CI） |
| 本方案文档 | `docs/14-...md` | 本文件 | ✅ |

## 6. 后续路线（建议排期）

**W10（下一轮，修复 + 收口）**——把本轮登记的风险按性价比排序修复：

1. **F2 流式上传**（高优）：`upload_audit` 改为分块读取累计计数，超 200MB 立即中断返回
   413，不再全量缓冲；A6 场景复跑验证 RSS 峰值回落。
2. **F5 准入控制**（高优）：`asyncio.Semaphore(max_running)` + 队列深度上限（超限 429），
   A3 场景补断言（超限请求被拒）。
3. **F4 沙箱输出限量**（中优）：_drain 读满上限字节即放弃剩余输出，防输出炸弹内存峰值。
4. **L2 金丝雀回放脚本**（中优）：`bench/canary/replay.py`，双版本双端口请求回放 diff。
5. **S9 soak 压测**（中优）：5–10 分钟混合负载 + RSS 采样曲线，判定无泄漏后再谈多 worker。
6. F1/F3 登记不修（本地工具设计边界），在 README 安全章节明示部署边界。

**W11+（形态演进）**：多 worker + 任务表持久化（SQLite/文件）→ 全套压测 / 并发 / 对抗
重跑出基线；在线 GLM 评估（等 key，含 A8 在线版注入鲁棒性）；Docker 沙箱执行器替换
（F3 根治）；报告 schema_version 升版演练（2.0 → 旧版渲染兼容矩阵）。

## 7. 运行速查

```bash
# 全量 pytest（含本轮新增 property / gray_release，约 3 分钟）
python -m pytest tests -q

# 对抗八场景（真实服务子进程 + 210MB 素材，约 4–6 分钟，离线）
python -m bench.adversarial.run_adversarial --port 8903 --out bench/results/adversarial_w9.md

# 既有压测（口径不变）
python -m bench.stress.run_stress          # 管线级六场景
python -m bench.stress.run_stress_web      # 服务级四场景
```
