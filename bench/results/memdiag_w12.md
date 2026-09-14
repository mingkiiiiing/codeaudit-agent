# W12-A4 服务端层内存归因报告（F9）——长窗 soak + 逐项排查 + 定向探针

> 2026-09-15 ｜ 执行：W12-A4 性能工程师 ｜ 契约：docs/17 §1.2 F9 / §2 W12-A4
> 新增产物：`bench/stress/run_soak_long.py`、`bench/memdiag/run_serverdiff.py`、
> `bench/memdiag/probe_server_state.py`（既有 run_soak.py / run_memdiag.py / server/ 零改动）

## 1. 最终结论（三选一）：**有界增长**

soak 观测的每任务 ~0.15-0.27 MB RSS 缓爬（W10/W11 DEFER）**不在** server/app.py 簿记层，
也不在 audit/* 库层（W11 memdiag 已证）；它由「一次性预热抬升 + 分配器高水位慢衰减」构成，
在 10 分钟窗与 40 任务差分下均收敛于平台期。上界公式（本机实测口径）::

    RSS_long ≈ 基线 73-74 MB（启动 + 首任务懒加载后）
             + 一次性预热 ≈ +9~10 MB（前 ~10 个任务内集中发生，差分首段占总增量 94-96%）
             + 稳态残差 ≤ ~13 KB/任务（40 任务差分实测趋零：段增量 30→10→9→0 KB/任务）
             ⇒ FIFO 兜底（50 终态任务）下长时运行上界 ≈ 85-90 MB 量级

长窗实测：600s 混合负载 100 任务后 RSS 83.2 MB 封顶，稳态斜率 0.546 MB/min（< 1，PASS），
且较 300s quick 窗（1.152 MB/min）**减半**——二阶导为负，收敛特征明确。无单点泄漏、无需修复。

## 2. 长窗 soak（bench/stress/run_soak_long.py，三态口径与 run_soak 完全一致）

判定口径：无 5xx / 终态率 100% / RSS 稳态斜率 < 1 MB/min（PASS，超限但与任务数 r≥0.6 判 DEFER）
/ 结束 total ≤ 50 / 离线金丝雀。任务提交周期 6s（run_soak 8s），RSS 采样 10s 粒度。

| 项 | 600s 标准窗（主判） | 300s quick 窗（参考，`soak_long_w12_quick.md`） |
|---|---|---|
| **总判定** | **PASS** | PASS（含 DEFER） |
| 提交 / 接纳 / 终态(done) | 100 / 100 / 100（终态率 100%） | 49 / 49 / 49（100%） |
| 5xx / 客户端异常 / 429 | 0 / 0 / 0 | 0 / 0 / 0 |
| SSE 连接 / done 终帧 / 错误 | 20 / 20 / 0 | 10 / 10 / 0 |
| RSS 首 → 末 | 73.1 → 83.2 MB（Δ+10.1） | 73.3 → 82.3 MB（Δ+9.0） |
| RSS 稳态斜率（剔除 60s 预热） | **0.546 MB/min（PASS）**，55 样本；全窗参考 0.665 | 1.152 MB/min（超限），r=0.89 → DEFER |
| 结束 total（FIFO ≤50） | 0 | 0 |
| 离线金丝雀（三重断言） | PASS | PASS |
| 读负载 | 2 Hz × 双端点 ×600s，p99 见报告 | 同左（300s） |

斜率 1.152 → 0.546 MB/min（窗口加倍后减半、r 仍 0.86-0.89）＝"随任务数强相关但增量递减"的
预热/高水位特征，与差分探针的平台期（§4）互证；时间泄漏特征（弱相关 + 线性爬升）不成立。

## 3. 服务端层逐项排查（读代码 + 进程内定向探针取证）

| # | 嫌疑 | 代码位置 | 证据（静态 + 探针） | 判定 |
|---|---|---|---|---|
| 1 | `_RUNNING`/`_TASKS` 句柄清理 | server/app.py:102/106（声明）、257-258（登记）、260-266（`_on_done`：`_TASKS.discard` + `_RUNNING.pop(is 守卫)`，`add_done_callback` 在异常/取消路径同样触发） | 探针并发 8 任务（>闸门容量 4，覆盖排队）全部终态后 `len(_RUNNING)==0`、`len(_TASKS)==0` | **排除** |
| 2 | `_RUN_GATE` 信号量泄漏 | server/app.py:87（容量 4）、221（acquire）、223-227（`acquired` 标志 + finally release；acquire 未返回时 `acquired=False`，无超额 release） | 排队路径结束后 `_RUN_GATE._value==4`（== 初始容量）；soak 长窗 100 任务 0 次 429 异常、无任务饿死 | **排除** |
| 3a | TaskStore 连接/游标生命周期 | audit/taskstore.py:84（**单连接** `check_same_thread=False` + 内部锁串行化）；各查询 `self._conn.execute(...)` 新建游标 → `fetchone/fetchall` 后行集即可回收（CPython 引用计数） | 探针静默期 `sqlite3.Connection` 存活 = **1**（_STORE 单例复用，无每查询新建连接）；`sqlite3.Cursor` 存活 = **0**（无钉死） | **排除** |
| 3b | events 表无限增长对读路径内存 | taskstore.py:221-242（append）、244-251（get_events 按 audit_id 过滤 + fetchall，单任务事件数有界）、172-178（delete 连带删 events）、285-307（prune 连带删 events——FIFO 淘汰不残留） | 探针：11 任务存活期 events=264 行 → 全量 DELETE 后 events=0、audits=0；读路径 `get_events(audit_id)` 只触单任务行集，表总量不进读路径内存。磁盘口径：events 只随存活任务数线性（≤50 任务 × 每任务 ~24 行） | **排除**（读路径内存有界；磁盘随存活任务有界） |
| 4 | SSE 生成器泄漏 | server/app.py:433-454（`event_stream`：`get_events` 局部列表 + 0.05s 轮询，终态/删除即 return）；sse_starlette 3.4.11 `__call__`：`cancel_on_finish` + `_listen_for_disconnect`——断开即 cancel task group（生成器 aclose） | 探针（真 uvicorn 子线程）：3 次客户端中途断开后残留 event_stream 异步生成器 = 0；正常消费至 done 终帧后残留 = 0；soak 长窗 20 条 SSE 全部干净收流（0 错误） | **排除** |
| 5 | uvicorn/FastAPI 请求日志缓冲 | uvicorn logging 配置（写透型 StreamHandler → stderr）；本系列 runner 将子进程输出重定向到临时文件（磁盘不占内存） | 探针：负载前后 logging handler 0→2（uvicorn 启动期一次性配置），内存缓冲型 handler（Memory/Queue/Buffering）= **0**；h11 每连接缓冲为请求级暂态 | **排除**（内存无累积；日志文件磁盘增长属磁盘占用，部署态建议按需轮转） |

## 4. 定向探针一：进程内状态取证（bench/memdiag/probe_server_state.py → probe_server_state_w12.md）

同进程加载 server/app.py，真 uvicorn 子线程（并发/排队/SSE 断开）+ ASGI transport（顺序请求）双形态，
10 项取证**全部通过**：句柄清理 ×2、闸门配对、离线三重断言、SSE 断开释放、SSE 正常收流、
events 归零、单例连接、游标无钉死、日志无缓冲。线程数 1→1（to_thread 默认线程池上限
min(32, cpu+4)，本负载下仅 3-5 线程，有界一次性扩容，见差分锚点线程数列）。

## 5. 定向探针二：HTTP 层差分（bench/memdiag/run_serverdiff.py → serverdiff_w12.md）

真服务子进程（完整 HTTP 栈：multipart 流式上传 → to_thread 任务线程 → SSE 可达），顺序 40 个
mini_app 任务（协议=上传→终态→DELETE，与 soak 同口径，多于 W11 库层 30 轮），锚点 RSS（psutil
工作集，两次运行复现）：

| 锚点 | 运行 A | 运行 B | 段增量（A） |
|---|---|---|---|
| 0 | 64.5 MB | 64.8 MB | - |
| 10 | 73.8 | 73.9 | +9.3 MB（+953 KB/任务，一次性预热） |
| 20 | 74.2 | 74.2 | +0.4 MB（+38 KB/任务） |
| 30 | 74.3 | 74.2 | +0.1 MB（+10 KB/任务） |
| 40 | 74.4 | 74.3 | +0.1 MB（+9 KB/任务） |
| 40+静置5s | 74.4 | 74.3 | ±0（无松弛也无爬升） |

首段占总增量 94-96%；稳态最小二乘 12.9-20.7 KB/任务且逐段衰减趋零——低于 50 KB/任务疑点阈值，
**差分法未复现** soak 的 150-270 KB/任务线性缓爬。离线断言 40/40 通过。

## 6. 归因分析：soak 缓爬的成分分解

1. **一次性预热 ≈ +9-10 MB**：首任务触发 audit/* 懒加载导入、to_thread 线程池扩容、
   sqlite WAL 页缓存填充（差分 0→10 锚点段；W11 memdiag 第 1 轮同观测）。
2. **分配器高水位慢衰减 ≈ 稳态 <1 MB/min 且递减**：每任务暂态大对象（报告 JSON 序列化/
   反序列化、proj80 80 文件树）抬升 pymalloc/MSVC 高水位，RSS 部分归还、部分驻留；
   随同类任务重复分配复用驻留页，增量逐段衰减（差分 30→10→9→0 KB/任务；soak 斜率
   1.152→0.546 MB/min 同为二阶导负）。tracemalloc 不可见（非 Python 对象滞留，与 W11
   库层结论一致）。
3. **soak 与差分口径差异的解释**：soak 交替 proj80（大报告 → 更高暂态水位）且 10 分钟窗
   仍处衰减段，故表现为 DEFER 级缓爬；差分纯 mini_app + 40 任务锚点已进入平台期。
   两者不矛盾，均指向同一有界机制。
4. **簿记层零贡献**：任务 DELETE/FIFO prune 后表行、事件、报告、句柄、闸门、SSE 生成器
   全部回收（§3/§4 取证），无可累积容器。

## 7. 修复建议（可选低优先级；本任务未改 server/ 任何文件）

- 无必须修复项。可选微调（供集成人裁决，均非缺陷）：
  1. `/api/health` 读路径调用 `store.list(0,0)` 取全表行仅为取一列计数（server/app.py:311）
     ——可改 `COUNT(*)`，省每请求 ≤50 行临时对象（有界，非泄漏）；
  2. SSE 轮询间隔 0.05s 偏密（server/app.py:452）——纯 CPU 空转项，非内存项，维持现状亦可；
  3. uvicorn access log 长期运行写文件无限增长——部署态配置日志轮转（磁盘卫生，非内存）。

## 8. 诚实边界

- 离线口径：父/子进程双 pop GLM_*；**服务子进程 CWD 为无 .env 的系统临时目录**（W12-A1/F7
  的 from_env 自动加载 CWD/.env 无从触发）+ CODEAUDIT_DB_PATH 指向临时 db + 金丝雀三重断言
  （db api_key ∈ {'','<redacted>'}（F6 脱敏）、终态报告 tokens 恒 0（FakeLLM 零用量签名）、
  CWD 无 .env）。注意：FakeLLM 的 `stats.llm_calls` 会正常计数，**不能**作为在线判据
  （本任务初期误设该断言，已修正并记录）。
- 探针期间 W12-A1（audit/config.py、server/app.py 的 F6/F7）并行合入：排查表行号以合入后
  工作树为准；F6 脱敏使 db 中 api_key 恒为 `"<redacted>"`，离线判据相应调整为三重断言。
- 差分探针跨进程不可用 tracemalloc/pympler，按契约定式退化为「静态排除 + 进程级差分 +
  同进程内省」三层证据组合；RSS 为 Windows 工作集口径（含分配器高水位，属"滞留+未归还"合并上界）。
- 600s 窗对小时级慢泄漏灵敏度仍有限；但差分 40 任务段增量已趋零 + 斜率减半，外推风险低。
- 上界公式为本机（Windows 11 / Python 3.13.9 / psapi 工作集）口径；绝对数值随机器/依赖版本浮动，
  有界性结论（一次性 + 趋零残差 + FIFO 兜底）不依赖绝对值。

## 9. 产物与复现

- `bench/results/soak_long_w12.md`（600s 标准窗，主判 PASS）；`soak_long_w12_quick.md`（300s 参考）
- `bench/results/serverdiff_w12.md`（40 任务差分，两次运行）；`bench/results/probe_server_state_w12.md`（进程内 10 项取证）
- 复现：`python -m bench.stress.run_soak_long [--quick]`；`python -m bench.memdiag.run_serverdiff`；
  `python -m bench.memdiag.probe_server_state`；`ruff check bench/stress/run_soak_long.py bench/memdiag` 零告警。
- 临时产物全部走系统临时目录并 finally 清理（报告内含清理日志行）。
