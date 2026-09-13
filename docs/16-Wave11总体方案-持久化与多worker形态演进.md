# Wave 11 总体方案——形态演进：任务持久化、多 worker、线程池执行与 RSS 归因

> 2026-09-14 ｜ 状态：开发中 ｜ 前置：docs/14（F1–F5 登记）、docs/15（W10 治理与灰度基建）
>
> 本 Wave 解决 W9/W10 压测暴露的三个形态限制：任务表内存态（重启即失、无法多 worker）、
> 审计任务跑在共享事件循环（CPU 密集段饿死循环，POST 响应被推迟、health 失联——A3 场景
> 25 路 denied=0 的根因）、以及每任务 ~0.15–0.27 MB 的 RSS 缓爬（r=0.68，疑似每任务缓存，
> 未归因）。在线 GLM 评估（等 key）与 Docker 沙箱不在本 Wave。

## 1. 契约 v2.2 微增（本 Wave 的行为变化，向后兼容）

### 1.1 任务持久化（TaskStore，SQLite）

- 新模块 `audit/taskstore.py`：`TaskStore(db_path)`——标准库 SQLite，WAL 模式，
  `check_same_thread=False` + 内部写锁（事件循环线程与任务工作线程并发访问）。
- 两张表：`audits`（audit_id PK / status / error / created_at / source_path / do_fix /
  do_tests / config_json / report_json）与 `events`（audit_id+seq 复合主键 / event_json）。
- server 的内存 `AUDITS` dict 移除，全部读写走 store；store 路径 env
  `CODEAUDIT_DB_PATH`（默认 `<work_root>/audits.db`）；测试经 `monkeypatch.setattr(server.app, "_STORE", ...)` 注入。
- **重启语义**：服务启动时 `sweep_interrupted()` 把遗留 queued/running 置
  failed（error=`服务重启中断`）——终态任务与报告重启后仍可查询与下载（新能力）。
- **FIFO 容量淘汰**迁移到 store（`prune(keep=50)`，只淘汰终态，语义不变）。

### 1.2 线程池执行 + 协作式取消

- 审计任务从共享事件循环迁到独立线程：`asyncio.to_thread(_run_audit_sync, ...)`，
  线程内 `asyncio.run(run_audit(config, emitter))`——每任务独立循环，CPU 密集段不再饿死
  服务循环（POST 响应、health、SSE 全程可响应）。
- **取消语义变化（诚实声明）**：DELETE 不再能瞬时打断线程，改为协作式——
  `request_cancel` 置标志；任务 emitter 在每次事件边界检查（标志置位 **或** 表项已被
  物理删除 → 抛 CancelledError，既有 BaseException 兜底落 failed）。取消延迟 =
  到下一个阶段事件边界的间隔（七阶段均频繁 emit，典型亚秒级）。`DELETE` 的 HTTP 语义
  不变（204 + 表项移除 + SSE 收流）。
- 429/gate：`count_active` 改走 store（多 worker 下即**全局**准确）；`_RUN_GATE`
  保持 per-worker（全局并发上限 = workers × 4），文档如实标注。
- 事件持久化后 SSE 轮询改从 store 按 seq 游标读取，回放/跟随语义不变。

### 1.3 多 worker（实验特性）

- `codeaudit serve --workers N`（默认 1）：N>1 时以 import string 形式启动
  `uvicorn.run("server.app:app", workers=N)`（实例形式与 workers>1 不兼容）。
- sticky 执行模型：任务由接收请求的 worker 执行；其他 worker 经 SQLite 只读可见
  （列表/详情/报告/SSE）。跨 worker DELETE 经 `request_cancel` 标志生效。
- 多 worker 为实验特性：单 worker 仍是默认与推荐形态；全套压测基线以单 worker 口径
  出具，多 worker 冒烟验证跨 worker 可见性即可。

## 2. 任务分解与目录所有权（四人并行 + 集成人收口）

| 任务 | 职务 | 所有权（只许改这些路径） | 交付物 | 验收 |
|---|---|---|---|---|
| W11-A1 | 存储层工程师 | `audit/taskstore.py`（骨架已给，填实现）、`tests/unit/taskstore/` | TaskStore 完整实现 + 单测 | 骨架全部签名落地；并发读写单测（线程池压 store）全绿 |
| W11-A2 | 服务端工程师 | `server/app.py`、`tests/unit/server/` | AUDITS→store、to_thread 执行、协作取消、SSE 游标化、启动 sweep、429 全局化 | 既有 server 用例全绿（允许按 1.2 调整取消相关断言）；新增重启恢复/跨线程取消用例 |
| W11-A3 | 入口与多 worker 工程师 | `cli.py`、`tests/unit/cli/` | `serve --workers N` + import string 启动 + 多 worker 冒烟验证（跨 worker 建/查/删） | 单 worker 行为不变；N=2 冒烟：worker0 建任务 worker1 可查可删 |
| W11-A4 | 内存诊断工程师 | `bench/memdiag/`（新建） | `run_memdiag.py`：N 个任务逐个 tracemalloc 快照 diff + RSS 采样，top 增长归因报告 | 报告落 `bench/results/memdiag_w11.md`；给出泄漏点定位或"无单点泄漏"结论 |
| W11-A5 | 集成人（主线） | `docs/`、`CHANGELOG.md`、`README.md`、`Makefile`、`bench/adversarial/`、`bench/stress/` | 契约、骨架、收口联调、三路复跑（adversarial/soak/canary）、修复、发布 | 五关全绿后 commit |

**依赖解耦**：A2 的单测不得依赖 A1 的实现——在 `tests/unit/server/` 自建内存版假 store
（实现 `audit/taskstore.py` 骨架的接口即可）；集成时全量跑真 store。A3/A4 与 A1/A2 无依赖。

## 3. 集成收口清单（W11-A5）

1. 全量 pytest + ruff；既有 1082 项中 server/CLI 相关用例按契约 1.1/1.2 适配（适配点须逐条记录）。
2. adversarial 复跑（重点：A4 抖动、A5 SSE DELETE 收流在协作取消下的语义、A6 上传、
   A3 准入的全局计数行为）；soak 复跑（RSS 归因结论回填判定）；canary 完整回放
   （v0.5.0 vs W11 HEAD——持久化不得改变报告语义）。
3. 重启恢复冒烟：建任务至 done → 重启服务 → 列表/报告仍可查。
4. 多 worker 冒烟（A3 交付）：--workers 2 跨 worker 可见性。
5. CHANGELOG / README / Makefile 收口，commit + push。

## 4. 风险与回退

- store 迁移是本 Wave 最大风险：既有 server 用例大量直接操作 `AUDITS` dict——允许 A2
  适配这些用例，但**断言语义不得放宽**（404/429/取消/淘汰语义逐条保留）。
- 协作式取消的延迟语义若被既有用例断言为"立即终态"，允许改为"轮询等待终态"（条件等待，
  禁固定 sleep）。
- 多 worker 仅冒烟不进压测基线；若 uvicorn spawn 模式在 Windows 出问题，如实记录并保持
  默认 workers=1。
