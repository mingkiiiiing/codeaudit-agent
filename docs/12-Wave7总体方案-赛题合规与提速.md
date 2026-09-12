# 12 Wave 7 总体方案：赛题合规收口与算法提速

版本：v1.0 ｜ 日期：2026-09-12 ｜ 前置：docs/11（v0.3.0 已发布，925 测试 + CI/Docs/Release 全绿）

---

## 1. 赛题合规审计（基线：项目立项描述，若另有正式赛题文档可再精确对照）

| # | 赛题要求 | 现状 | Wave 7 动作 |
|---|---|---|---|
| 1 | 上传项目代码文件夹 | ✅ zip/目录双入口 | — |
| 2 | 自动遍历文件、理解整体架构 | ✅ ingest+index+understand（架构卡片） | — |
| 3 | 自动检测 bug、性能问题、规范问题 | ✅ 63 条规则 + LLM 双通道 | — |
| 4 | 自动生成修复代码 | ✅ fix 阶段（Python 沙箱验证闭环） | — |
| 5 | **自动生成重构方案** | ❌ **唯一功能缺口**：仅有逐条 suggestion 文本，无系统性重构方案输出 | W7-A2 新增重构方案生成器（确定性聚合 + LLM 深度建议，报告独立章节） |
| 6 | 自动生成单元测试用例 | ✅ testgen（Python） | — |
| 7 | 输出完整审计报告 | ✅ md/html/json + SARIF + 健康分 | W7-A2 报告增加「重构方案」章节 |
| 8 | 支持主流编程语言 | ⚠️ Python 全闭环；JS/TS 仅检测 | W7-A3 JS/TS 修复与验证闭环（node --check / node --test） |
| 9 | 千行 <30s | ✅ 规则通道 0.245 s/KLOC；LLM 通道待真实评估 | W7-A1 提速（ingest 硬链接 + 规则并行），放大余量 |
| 10 | 准确率 85%+ | ⏳ 金标 240 条就绪，**等 GLM_API_KEY 真跑**（唯一外部依赖） | — |

## 2. 性能审计（W6 压测已锁定，本轮修）

| 热点 | 现状（2000 文件档） | 目标 |
|---|---|---|
| 规则扫描单线程 | 50.9s（纯 CPU 串行） | ProcessPool 分片并行 → ≤20s |
| ingest 全树复制 | 8.3s（copytree+逐文件 sha） | 同盘 os.link 硬链接（跨盘回退 copy）→ ≤3s |
| LLM 通道 | 并发 8 已可配；批量切片已有 | 保持（真实提速靠 key 到位后的并发与缓存） |

## 3. 任务分解表（4 个大任务，两批并行）

| 代号 | 任务名 | 独占目录/文件 |
|---|---|---|
| W7-A1 | 算法提速 | `audit/detect/engine.py`（并行引擎）、`audit/ingest/core.py`（硬链接）、`bench/stress/**`（对比复跑）、对应测试 |
| W7-A2 | 重构方案生成器 | `audit/refactor/**`（新模块）、`audit/models.py`（契约 v1.7 微增）、`audit/pipeline.py`、`audit/report/**`、`audit/orchestrator/pipeline.py`（接 Stage）、对应测试 |
| W7-A3 | JS/TS 修复与验证闭环 | `audit/fix/**`、`audit/testgen/**`、`audit/sandbox/executor.py`（node 白名单）、对应测试 |
| W7-A4 | 赛题对照与发布 0.4.0 | `docs/12` 合规矩阵节、`README.md`、`CHANGELOG.md`、`audit/__init__.py`、`docs-site/**`、`mkdocs.yml` |

契约 v1.7 微增（A2 落地）：`RefactorProposal` 数据类（id/title/target/kind/rationale/steps/related_issues/source）+ `AuditReport.refactor_proposals` + `PipelineContext.refactor_proposals`。

## 4. 关键设计约束

1. **A1 并行规则引擎必须行为等价**：规则命中集合与串行版完全一致（现有全部规则测试 + demo_proj 金标命中作为等价性断言）；Windows spawn 安全（worker 为模块级函数、入参可 pickle：rel_path+source+symbols 基本类型）；`RuleContext.tree` 在 worker 内重建或置 None（现有规则均不依赖 tree——已核实）；并发度可配（`rule_scan_workers`，默认 `min(4, cpu)`），ProcessPool 仅在文件数 > 100 时启用（小项目进程开销不划算）。ingest 硬链接需处理跨盘回退与 copytree ignore 语义保留。
2. **A2 重构方案的确定性底座 + LLM 增强**：确定性层零 LLM 可出（长函数 top-N 分解、重复代码聚类、热点模块拆分、循环依赖提示——全部从已有索引/规则命中聚合）；LLM 层增强每个方案的 rationale/steps（FakeLLM 可测，降级安全）。报告三格式 + SARIF 不含重构项（SARIF 只放 Issue）。
3. **A3 JS/TS 闭环的诚实降级**：语法验证走 tree-sitter 重解析（已有能力）+ node --check（探测可用才加）；单测验证走 `node --test`（node 18+ 内置，探测可用才执行，不可用 → syntax-ok 诚实标注）；CI（ubuntu/windows runner 自带 node）实证。
4. 底线：全量 pytest 零回归（基线 925+新增）、ruff 零告警、demo exit 0、tests/integration 全绿、压测复跑出对比数据。

## 5. 集成与发布流程（集成人）

四任务合流 → 全量 + 联调 + 压测复跑（出 W7 vs W6 对比表）→ dogfood 复扫 → 修复 → 推送 → CI 绿 → tag **v0.4.0** → Release 确认。

## 7. 发布记录（W7-A4，v0.4.0）

2026-09-12 发布 0.4.0。§1 合规矩阵 10 项的**最终状态**全部落定：

| # | 赛题要求 | 最终状态 | 一句话证据 |
|---|---|---|---|
| 1 | 上传项目代码文件夹 | ✅ | zip / 目录双入口，接入失败门控（`tests/integration/test_zip_diff_fallback.py`） |
| 2 | 自动遍历文件、理解整体架构 | ✅ | ingest + index + understand 架构卡片，2000 文件档全流程压测通过（`bench/results/stress_w7_clean.md`） |
| 3 | 自动检测 bug、性能问题、规范问题 | ✅ | 静态规则库 **63 条** + LLM Review/Verify 双通道（规则手册 `docs-site/rules.md`） |
| 4 | 自动生成修复代码 | ✅ | fix 阶段 unified diff + `git apply` / 语法重解析 / 现有测试三重验证（`tests/integration/test_fix_tests_loop.py`） |
| 5 | **自动生成重构方案** | ✅ **重构方案已由 W7-A2 补齐** | `audit/refactor`（确定性启发式 + LLM 增强），报告新增「重构方案」章节，契约 v1.7 `RefactorProposal`（`tests/unit/refactor/`） |
| 6 | 自动生成单元测试用例 | ✅ | testgen 生成 pytest 用例并在沙箱运行，失败带 traceback 重试（同上联调用例） |
| 7 | 输出完整审计报告 | ✅ | md / html / json + SARIF + 健康分，0.4.0 起报告含「重构方案」章节 |
| 8 | 支持主流编程语言 | ✅ **JS/TS 修复与单测验证闭环由 W7-A3 补齐** | JS/TS 问题进入 fix 闭环，`node --check` 语法验证 + `node --test` 单测验证（探测可用才执行，不可用诚实标注）（`audit/fix/verifier.py`） |
| 9 | 千行 <30s | ✅ **规则通道 0.245 s/KLOC 且提速开关化** | W6 基线 0.245 s/KLOC 远优于 30 s/KLOC（`bench/results/stress_20260912.md`）；W7 复跑与串/并对拍见 `bench/results/stress_w7_clean.md` |
| 10 | 准确率 85%+ | ⏳ 等 key | 金标 240 条（10 项目集）就绪，唯一外部依赖 `GLM_API_KEY` 真跑（`bench/real_run`） |

**提速实验的诚实结论（W7-A1）**：规则并行与 ingest 硬链接均落地为**可选开关**（`rule_scan_workers` 默认串行、`link_same_volume` 默认复制）。本机 2000 文件档实测：并行对拍仅 -7.1%、硬链接路径相对复制 +70.3%（跨卷回退属正确行为），负收益已诚实回退默认值；§2 设计保留，结论以 `bench/results/stress_w7_clean.md` 与后续复跑为准。规则通道 0.245 s/KLOC（W6 基线）仍满足 <30 s/KLOC 目标 100 倍余量。
