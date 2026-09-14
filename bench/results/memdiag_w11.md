# W11-A4 库层内存诊断报告（memdiag）

## 环境头

- 生成时间：2026-09-14T12:43:35
- Python：3.13.9；平台：win32
- 轮数：30
- 审计素材：demo/mini_app（5 个文件，固定审计素材）
- 调用方式：库层直调 `asyncio.run(run_audit(config, noop_emitter))`，不经 HTTP（HTTP 层 RSS 已由 soak 覆盖）
- 每轮独立 work_root（临时目录，模拟服务端逐任务隔离，结束后已清理）；enable_llm_review=False（纯规则模式）
- 离线消毒：启动前已 pop GLM_API_KEY / GLM_BASE_URL / GLM_MODEL
- RSS 口径：Windows psapi.GetProcessMemoryInfo 当前进程工作集（与 soak 同口径）
- tracemalloc 口径：第 2 轮起轮前/轮后各 take_snapshot（depth=25），轮前 gc.collect()；diff=compare_to('lineno')

## 每轮 RSS / tracemalloc 采样

| 轮 | 耗时(s) | RSS(MB) | ΔRSS(MB) | 峰值WS(MB) | traced当前(MB) | traced峰值(MB) | issues | 轮内 top1 增长行 |
|---|---|---|---|---|---|---|---|---|
| 1 | 0.83 | 54.2 | - | 54.2 | - | - | 1 | - |
| 2 | 3.52 | 54.2 | +0.00 | 56.1 | 0.04 | 0.41 | 1 | C:/Anaconda/Lib/site-packages/pygments/formatters/html.py:514 +9.8KB |
| 3 | 3.87 | 55.0 | +0.80 | 56.1 | 0.04 | 0.41 | 1 | C:/Anaconda/Lib/site-packages/pygments/formatters/html.py:514 +9.8KB |
| 4 | 3.69 | 55.2 | +0.21 | 56.1 | 0.04 | 0.41 | 1 | C:/Anaconda/Lib/site-packages/pygments/formatters/html.py:514 +9.8KB |
| 5 | 3.65 | 55.8 | +0.60 | 58.0 | 1.87 | 2.24 | 1 | C:/Anaconda/Lib/pathlib/_local.py:274 +1877.4KB |
| 6 | 3.66 | 55.3 | -0.57 | 58.0 | 0.03 | 0.40 | 1 | C:/Anaconda/Lib/site-packages/pygments/formatters/html.py:514 +9.8KB |
| 7 | 3.60 | 55.2 | -0.05 | 58.0 | 0.04 | 0.40 | 1 | C:/Anaconda/Lib/site-packages/pygments/formatters/html.py:514 +9.8KB |
| 8 | 3.72 | 55.3 | +0.09 | 58.0 | 0.03 | 0.40 | 1 | C:/Anaconda/Lib/site-packages/pygments/formatters/html.py:514 +9.8KB |
| 9 | 3.56 | 56.4 | +1.07 | 58.0 | 0.04 | 0.40 | 1 | C:/Anaconda/Lib/site-packages/pygments/formatters/html.py:514 +9.8KB |
| 10 | 4.03 | 56.4 | +0.01 | 58.0 | 0.03 | 0.40 | 1 | C:/Anaconda/Lib/site-packages/pygments/formatters/html.py:514 +9.8KB |
| 11 | 3.54 | 56.3 | -0.07 | 58.0 | 0.04 | 0.40 | 1 | C:/Anaconda/Lib/site-packages/pygments/formatters/html.py:514 +9.8KB |
| 12 | 3.63 | 56.3 | +0.01 | 58.0 | 0.03 | 0.40 | 1 | C:/Anaconda/Lib/site-packages/pygments/formatters/html.py:514 +9.8KB |
| 13 | 3.52 | 56.4 | +0.04 | 58.0 | 0.03 | 0.40 | 1 | C:/Anaconda/Lib/site-packages/pygments/formatters/html.py:514 +9.8KB |
| 14 | 3.58 | 56.3 | -0.04 | 58.0 | 0.03 | 0.40 | 1 | C:/Anaconda/Lib/site-packages/pygments/formatters/html.py:514 +9.8KB |
| 15 | 3.52 | 56.3 | +0.02 | 58.0 | 0.03 | 0.40 | 1 | C:/Anaconda/Lib/site-packages/pygments/formatters/html.py:514 +9.8KB |
| 16 | 4.15 | 56.4 | +0.02 | 58.0 | 0.03 | 0.40 | 1 | C:/Anaconda/Lib/site-packages/pygments/formatters/html.py:514 +9.8KB |
| 17 | 4.40 | 56.3 | -0.03 | 58.0 | 0.03 | 0.40 | 1 | C:/Anaconda/Lib/site-packages/pygments/formatters/html.py:514 +9.8KB |
| 18 | 3.83 | 56.3 | -0.02 | 58.0 | 0.04 | 0.40 | 1 | C:/Anaconda/Lib/site-packages/pygments/formatters/html.py:514 +9.8KB |
| 19 | 3.87 | 56.3 | +0.03 | 58.0 | 0.03 | 0.40 | 1 | C:/Anaconda/Lib/site-packages/pygments/formatters/html.py:514 +9.8KB |
| 20 | 3.73 | 56.3 | +0.00 | 58.0 | 0.03 | 0.40 | 1 | C:/Anaconda/Lib/site-packages/pygments/formatters/html.py:514 +9.8KB |
| 21 | 3.43 | 56.4 | +0.06 | 58.0 | 0.03 | 0.40 | 1 | C:/Anaconda/Lib/site-packages/pygments/formatters/html.py:514 +9.8KB |
| 22 | 3.27 | 56.4 | -0.02 | 58.0 | 0.03 | 0.40 | 1 | C:/Anaconda/Lib/site-packages/pygments/formatters/html.py:514 +9.8KB |
| 23 | 3.36 | 56.4 | +0.05 | 58.0 | 0.03 | 0.40 | 1 | C:/Anaconda/Lib/site-packages/pygments/formatters/html.py:514 +9.8KB |
| 24 | 3.31 | 56.4 | -0.06 | 58.0 | 0.03 | 0.40 | 1 | C:/Anaconda/Lib/site-packages/pygments/formatters/html.py:514 +9.8KB |
| 25 | 3.17 | 56.4 | +0.05 | 58.0 | 0.03 | 0.40 | 1 | C:/Anaconda/Lib/site-packages/pygments/formatters/html.py:514 +9.8KB |
| 26 | 3.42 | 56.4 | +0.02 | 58.0 | 0.03 | 0.40 | 1 | C:/Anaconda/Lib/site-packages/pygments/formatters/html.py:514 +9.8KB |
| 27 | 3.40 | 56.4 | -0.05 | 58.0 | 0.03 | 0.40 | 1 | C:/Anaconda/Lib/site-packages/pygments/formatters/html.py:514 +9.8KB |
| 28 | 3.48 | 56.4 | +0.01 | 58.0 | 0.03 | 0.40 | 1 | C:/Anaconda/Lib/site-packages/pygments/formatters/html.py:514 +9.8KB |
| 29 | 3.29 | 56.5 | +0.12 | 58.0 | 0.03 | 0.40 | 1 | C:/Anaconda/Lib/site-packages/pygments/formatters/html.py:514 +9.8KB |
| 30 | 3.22 | 56.4 | -0.09 | 58.0 | 0.03 | 0.40 | 1 | C:/Anaconda/Lib/site-packages/pygments/formatters/html.py:514 +9.8KB |

## RSS 斜率与相关性

- 全轮 RSS 斜率：+0.058 MB/轮（折算 ≈ +0.985 MB/min，平均轮时长 3.51s）
- 稳态（第 6 轮起，25 轮）RSS 斜率：+0.032 MB/轮（折算 ≈ +0.546 MB/min）
- RSS 与轮次 Pearson r：0.76（soak DEFER 阈值 0.6）
- RSS 总变化：54.2 → 56.4 MB（Δ +2.2 MB，全程 109s）；第 1 轮含懒加载导入预热，解读以稳态段为准

## tracemalloc 文件级跨轮累计聚合（top 15）

| 文件 | 累计净增(KB) | 轮均(KB) | 前3轮占比 | 后5轮轮均(KB) | 判定 |
|---|---|---|---|---|---|
| C:/Anaconda/Lib/pathlib/_local.py | +2092.3 | +72.15 | 1% | +7.563 | 线性增长 |
| C:/Anaconda/Lib/site-packages/pygments/formatters/html.py | +474.5 | +16.36 | 11% | +15.322 | 线性增长 |
| C:/Anaconda/Lib/importlib/metadata/_collections.py | +62.5 | +2.16 | 10% | +2.156 | 线性增长 |
| audit/understand/architecture.py | +59.9 | +2.06 | 11% | +1.973 | 线性增长 |
| audit/detect/rules/python.py | +39.3 | +1.35 | 21% | +1.138 | 线性增长 |
| C:/Anaconda/Lib/importlib/metadata/__init__.py | +37.7 | +1.30 | 12% | +1.257 | 线性增长 |
| audit/detect/engine.py | +35.0 | +1.21 | 11% | +1.177 | 线性增长 |
| audit/report/builder.py | +15.7 | +0.54 | 12% | +0.508 | 线性增长 |
| audit/detect/rules/_python_common.py | +13.4 | +0.46 | 28% | +0.312 | 线性增长 |
| C:/Anaconda/Lib/tracemalloc.py | +9.8 | +0.34 | 20% | +0.219 | 线性增长 |
| audit/report/render.py | +9.1 | +0.31 | 10% | +0.312 | 线性增长 |
| audit/models.py | +7.5 | +0.26 | 15% | +0.234 | 线性增长 |
| C:/Anaconda/Lib/site-packages/pygments/lexer.py | +6.1 | +0.21 | 10% | +0.209 | 线性增长 |
| audit/utils.py | +6.0 | +0.21 | 11% | +0.213 | 线性增长 |
| bench/memdiag/run_memdiag.py | +4.8 | +0.16 | 10% | +0.164 | 平台期波动 |

判定规则：稳态（后 5 轮）轮均净增 >0.2 KB/轮 →「线性增长」（真滞留信号）；前 3 轮占累计 ≥80% 且稳态归零 →「预热集中」（缓存/预热，有界）；其余为「平台期波动」。

## 嫌疑清单核查

| 嫌疑点 | 证据 | 有界性判定 | 建议 |
|---|---|---|---|
| audit/indexer/parsers.py:19/31 lru_cache（Language/Parser） | maxsize=None 按语言名缓存；键空间=3 种语言，实测 currsize 见盘点节 | 有界（≤3 条目） | 无需修复 |
| audit/indexer/store.py:142 _alias_cache / _lang_map（实例级） | 实例随 SqliteIndexStore 创建，build 前重置（store.py:433-436）；连接与实例生命周期见盘点节 | 有界（任务级实例，finally close） | 若盘点节末态存活 >0 则需排查外部引用链 |
| audit/indexer/store.py:130 SQLite 连接与语句缓存 | 每轮新建连接；orchestrator/pipeline.py finally 统一 close（R1-4 收口） | 有界（连接随任务关闭） | 盘点节 sqlite3.Connection 末态存活数为直接证据 |
| tree-sitter Parser 实例（parsers.py） | 经 lru_cache 复用 3 个 Parser 实例；tree 对象随任务 GC | 有界 | 无需修复 |
| audit/detect/rules 正则编译缓存 | 绝大多数为模块级/类级一次性编译；python_ext.py:557 循环内按变量名动态 re.compile，进入 re 模块全局缓存（512 条上限，实测见盘点节） | 有界（512 条上限）但动态键会占满缓存 | 建议（集成人裁决）：python_ext.py 局部变量赋值检测改为预编译模板 + 子模式复用，避免动态 pattern 污染 re 全局缓存（约数十 KB 量级一次性占用） |
| audit/llm/glm_client.py:57 _cache（响应缓存） | 本诊断 enable_llm_review=False → FakeLLM，GlmClient 未构造；HTTP 服务场景为 FIFO 容量 4096 上限 | 有界（FIFO 4096；且为任务级实例） | 无需修复；若服务端未来跨任务共享 GlmClient，需复核 4096×单响应体上限 |
| audit/llm/base.py FakeLLMClient.calls（调用记录） | 实例级列表，随 PipelineContext/ctx 一起可回收 | 有界（任务级） | 无需修复 |
| audit/report/builder.py 模块级状态 | 仅有 _LLM_STAT_KEYS 等常量元组，无可变模块级容器 | 有界 | 无需修复 |
| asyncio 循环每轮 asyncio.run 新建销毁 | asyncio.run 退出时 shutdown_asyncgens/default_executor 后 close；残留见盘点节 Loop 计数 | 有界（无跨轮 Loop 累积） | RSS 层面的残差属 CPython/MSVC 分配器高水位，tracemalloc 不可见，非 Python 对象泄漏 |
| logging handler 累积 | grep 全 audit/ 无 getLogger/basicConfig/addHandler 调用（仅规则夹具字符串字面量）；实测见盘点节 | 有界 | 无需修复 |
| audit/detect/engine.py ProcessPoolExecutor（W7 并行扫描） | mini_app 5 文件 < PARALLEL_SCAN_MIN_FILES(100) 且默认 rule_scan_workers=1 → 本诊断走串行路径 | 本路径未触发 | 大库（≥100 文件）+ 并行开启时才有进程池；executor.shutdown(wait=True) 已在 finally 收口 |

## 运行末残留盘点（全部轮次结束后）

- sqlite3.Connection（索引库连接）：无残留
- SqliteIndexStore（索引存储实例）：无残留
- WorkspaceContext（工作区上下文）：无残留
- PipelineContext（流水线上下文）：无残留
- GlmClient（真实 LLM 客户端）：无残留
- FakeLLMClient（离线假客户端）：无残留
- 事件循环对象（asyncio Loop）残留：0 个
- 存活线程数（含主线程）：1 个
- logging 全局 handler 总数（root + 具名 logger）：0 个
- re 模块编译缓存条目（上限 512）：512 条
- audit/indexer/parsers.py lru_cache：language currsize=1（hits=0），parser currsize=1（hits=239）——按语言数有界（≤3）

## 结论与修复建议

- 检出 Python 对象级线性滞留文件 14 个（见聚合表「线性增长」行），为每任务缓爬的直接嫌疑，建议按文件逐点复核引用链后修复。
- RSS 维度：稳态斜率 +0.032 MB/轮 ≈ 0——库层（纯规则模式）RSS 无每任务常数缓爬。
- soak 每任务 ~0.15-0.27 MB 缓爬若在本诊断（纯规则库层）不复现，则增量大概率来自 HTTP 层/服务端任务簿记（taskstore 历史表、SSE 通道、报告文件句柄缓存等），归集成人在 server/ 侧复核。
- 修复建议汇总见「嫌疑清单核查」表「建议」列；本任务只诊断不修（audit/* 所有权约束）。
