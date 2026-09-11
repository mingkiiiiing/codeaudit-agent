# 10 Wave 5 总体方案：质量攻坚（审查 → 修复 → 联调 → 压测）

版本：v1.0 ｜ 日期：2026-09-12 ｜ 前置：docs/09（Wave 4 已发布，737 测试 + CI 全绿 @ e8ec7aa）

---

## 1. 审查结论（本轮起点）

三路只读审查 + Dogfood 自审计（用本工具审计自身第一方源码）共产出 **68 条发现**，全部有实证：

| 来源 | 数量 | 最重要的发现 |
|---|---|---|
| R1 代码质量 | 30（C1/H4/M13/L12） | **R1-1 critical**：`_make_review_fn` 参数错绑，simple 模式 LLM 审查完全失效且报告谎报 rule+llm（运行时实证）；R1-2 ingest 失败无门控会向用户原始项目 git init 打补丁；R1-3/R1-4 GlmClient 与 SqliteIndexStore 连接永不关闭；R1-6 server AUDITS 无界增长 |
| R2 测试缺口 | 22 + 10 必测场景 | tests/integration 不存在；detect→fix→testgen 闭环只有 demo 证明没有 pytest 用例；CLI/API 与真实流水线的联调为零；退出码契约未固化 |
| R3 文档一致性 | 13 | R3-1 high：文档未提醒 `.codeaudit/` 必须忽略（实测复跑会产生 5 层嵌套副本、168 个假问题）；其余为过时/瑕疵，多数文档经实测为一致 |
| Dogfood | 6 | 无路径排除功能；store.py 循环逐行 execute；PY-HARDCODED-SECRET 误报（`_FINGERPRINT_KEY` 常量命中 critical） |

完整清单见各审查报告（R1-n/R2-n/R3-n 编号在本方案中被直接引用）。

## 2. 目标与验收

| 目标 | 验收 |
|---|---|
| G-A 修复审查发现的核心问题 | R1 Top5 + 全部 high + 指定 medium 修复完成；737 存量测试零回归；demo exit 0 |
| G-B 联调测试套件 | tests/integration ≥10 个场景全绿（R2 十场景全覆盖），整体 <5 分钟，全离线 |
| G-C 压力测试基线 | bench/stress 一键产出：2000 文件级合成项目的 ingest/index/审计吞吐（s/KLOC）、并发审查、预算熔断、内存数据报告，瓶颈入清单 |
| G-D 文档归零漂移 | R3 清单全部修复；README/docs-site 示例逐条可执行 |
| 底线 | 推送后 CI 三矩阵全绿 |

## 3. 任务分解表（4 个大任务，两批并行）

| 代号 | 任务名 | 独占目录/文件 | 核心输入 |
|---|---|---|---|
| W5-A1 | 联调测试套件 | `tests/integration/**` | R2 清单（10 场景为骨架） |
| W5-A2 | 压力测试与性能基线 | `bench/stress/**`、`bench/results/stress_*.md` | R1-8、Dogfood 性能发现为测试点 |
| W5-A3 | 核心修复 | `audit/**`、`server/app.py`、`cli.py`、`audit/workspace.py` | R1 清单 + R3-1 根因 + R3-7/R3-12 |
| W5-A4 | 文档归零 | `README.md`、`docs-site/**`、`CHANGELOG.md`、`.env.example`、`mkdocs.yml`、`pyproject.toml`(ruff exclude)、`.gitignore` | R3 清单 |

**协调机制（A1×A3 的 xfail 协议）**：A1 的联调用例一律断言"正确行为"；当前代码不满足的，标 `@pytest.mark.xfail(strict=True, reason="ISSUE-R1-n")`。A3 按清单修复。集成阶段由主控逐个验证后移除 xfail 标记——xfail 全部转绿是本波次的完成标志之一。

## 4. 修复清单（W5-A3 的范围约定）

**必修（按序）**：R1-1（参数错绑，附贯通集成测试要求）、R1-2（ingest_ok 门控）、R1-3+R1-4（资源收口：llm.aclose + store.close，FakeLLM 补空 aclose）、R1-5（按 diff 目标全量备份回滚）、R1-6（AUDITS 终态清理）、R1-7（create_task 持引用）、R3-1 根因（DEFAULT_IGNORE_DIRS 增补 `.codeaudit`）、R3-12（门禁消息走 stderr）、R3-7（prog 名）、R1-9（SECRET 规则词边界+熵校验，同步更新该规则单测并保证金标对齐率不降）、R1-10（统计排除 tests/generated）、R1-11（copytree ignore + 残缺 .git 骨架清理）、R1-13（run_tests target 拒绝 '-' 前缀）、R1-16（ingest 失败清理 task_root）、R1-17（audit_id 贯通）、R1-22（confkit ref 拒绝 '-'）。

**择机（S 级顺手修）**：R1-14、R1-15、R1-18、R1-20、R1-21、R1-23、R1-25、R1-26、R1-27、R1-29、R1-30、R1-12（正则长度上限）。

**记录不修（写进 docs/10 §6 已知限制）**：R1-8（索引 N+1 预取，交 A2 压测量化后视数据决定）、R1-19（事件结构化 status，前端联动，roadmap）、R1-24（server 可审计根白名单，部署问题）、R1-28（进程树回收，Windows Job Object，roadmap）。

## 5. 协作规约（继承 docs/06~09）

1. A1/A2 **只加测试不改业务代码**；发现的 bug 一律入清单（A1 用 xfail 标注）。
2. A3 是唯一修改 `audit/**` 与 `server/app.py`、`cli.py` 的人；每修一条必须附最小回归测试（放对应 tests/unit 子目录）；PY-HARDCODED-SECRET 调参后 `tests/unit/detect` 与金标对齐测试必须保持绿。
3. A4 不碰任何 .py（pyproject 仅 ruff exclude 增补行除外）。
4. 底线：全量 pytest 零回归、ruff 零告警、demo exit 0、CI 全绿。

## 6. 已知限制（本轮明确不修，写入文档）

- R1-8 索引 N+1 查询：待 A2 压测数据量化后列入下一波次。
- R1-19 事件无结构化 status 字段（前端靠文案正则判定阶段）：涉及前后端联动，roadmap。
- R1-24 server 可审计路径无白名单：演示服务定位，生产部署需反向代理/容器隔离，README 已有定位说明。
- R1-28 沙箱超时不回收进程树：Windows Job Object 方案，roadmap。
- .env 不自动加载（R3-3）：文档明示，产品化 dotenv 列入 roadmap。
