# 07 Wave 2 总体方案与任务分解

版本：v1.0 ｜ 日期：2026-09-11 ｜ 前置：docs/06（Wave 1 已交付，334 测试全绿，端到端纯规则模式跑通）

---

## 1. 现状与 Wave 2 目标

Wave 1 已达成：七阶段流水线骨架、双通道检测（规则通道完整、LLM 通道有简化路径）、报告三格式、CLI/API/Web、bench 指标计算。缺口：**Stage 5/6 是占位**、LLM 通道未走工具取证、JS/TS 只有解析无规则、简历三个数字（85%/30s/70%）尚无真实实验背书。

Wave 2 四大目标（对应总计划 W3~W6）：

| 目标 | 内容 | 对应简历亮点 |
|---|---|---|
| G-A 检测→修复→单测全闭环 | Stage 5 Fix（Patch 生成+沙箱验证）与 Stage 6 TestGen（生成+重试+剔除）真实落地 | "自动生成修复代码/重构方案、自动生成单元测试用例" |
| G-B LLM 通道增强 | 工具取证式审查（SimpleAgentRuntime 路径）、Verify 复核接入、文件级并发与批量切片 | "跨文件逻辑理解、准确率 85%+" |
| G-C 真实指标实测基建 | 金标扩充工具（git 历史回溯/缺陷注入）、消融真实执行、run 记录、真跑脚本（用户提供 key） | "85% / <30s / 70% 三个数字可复现" |
| G-D 多语言与产品化 | JS/TS 规则库、README、Web 报告交互升级、CLI 完善 | "支持主流编程语言" |

## 2. 里程碑（2 周）

| 周 | 内容 | 验收 |
|---|---|---|
| W2-周1 | 契约 v1.2（集成人）→ A1~A6 并行开发 → 集成联调 | 全量测试绿；demo_proj 上 --fix --tests 端到端（FakeLLM 脚本）产出 verified Patch + 通过的单测 |
| W2-周2 | 用户提供 GLM_API_KEY → 真跑评估（金标集 + 消融 + 耗时）→ prompt 调优迭代 → README/演示定稿 | bench 产出 run 记录与指标表，简历数字有据可查 |

## 3. 契约 v1.2（由集成人先行落地，各任务只消费不修改）

### 3.1 AuditConfig 新增字段

```python
enable_verify: bool = True        # 检测后启用 Verify Agent 复核（LLM 可用时才生效）
review_mode: str = "simple"       # simple=单次 json 调用 | tools=工具取证循环（SimpleAgentRuntime）
fix_max_patches: int = 50         # 单次审计最多生成修复 Patch 数
testgen_max_functions: int = 30   # 单次审计最多生成单测的目标函数数
batch_small_slices: bool = True   # 低风险小切片合并批量审查（省 token）
```

### 3.2 新阶段接口约定（Orchestrator 按 ImportError 降级调用）

```python
# audit/fix/__init__.py 暴露（W2-A1 实现）
async def run_fix_stage(ctx: PipelineContext) -> None
# 职责：取 ctx.issues 中 critical/high（medium 按预算）→ Fix Agent 生成 unified diff
# → git apply --check/apply → 语法重解析 → 跑现有测试（如有）
# → 更新 issue.fix_status（patch_generated/verified/needs-review/syntax-ok）+ ctx.patches

# audit/testgen/__init__.py 暴露（W2-A2 实现）
async def run_testgen_stage(ctx: PipelineContext) -> None
# 职责：对 verified Patch 涉及的目标函数生成 pytest 用例（写入 src 内 tests/generated/）
# → 沙箱运行 → 失败带 traceback 重试 ≤2 → 仍失败剔除 → ctx.test_cases

# audit/agents/review.py 暴露（W2-A3 实现）
def make_tools_review_fn(ctx: PipelineContext) -> review_fn   # review_mode="tools" 时由编排注入
# review_fn 签名不变：(workspace, file_path, hints) -> list[Issue]（同步/async 兼容）

# audit/detect/engine.py 扩展（W2-A3 实现，可选参数，向后兼容）
async def run_detection(ctx, review_fn=None, verify_fn=None) -> list[Issue]
# verify_fn 签名：(workspace, issue) -> Issue；confirmed 原样/更新置信度，
# false_positive 置 confidence=0（最终 ctx.issues 剔除），uncertain 降置信度保留
```

编排层已改为：模块存在则调真实现，缺失/未启用时保持原事件文案（`skipped: wave2` / `跳过（未启用 --fix）`）。

## 4. 任务分解表（6 个大任务，全部并行）

| 代号 | 任务名 | 独占目录/文件 | 前置 |
|---|---|---|---|
| W2-A1 | 修复闭环（Stage 5） | `audit/fix/**`、`tests/unit/fix/**` | 契约 v1.2 |
| W2-A2 | 单测生成闭环（Stage 6） | `audit/testgen/**`、`tests/unit/testgen/**` | 契约 v1.2 |
| W2-A3 | LLM 双通道增强 | `audit/agents/**`、`audit/detect/engine.py`、`tests/unit/agents/**`、`tests/unit/detect/test_verify_wiring*.py` | 契约 v1.2 |
| W2-A4 | JS/TS 规则库 | `audit/detect/rules/js/**`、`audit/detect/rules/__init__.py`、`audit/detect/registry.py`、`tests/unit/detect/test_rules_js*.py` | 契约 v1.2 |
| W2-A5 | 评估实测基建 | `bench/**`、`tests/unit/bench/**` | 契约 v1.2 |
| W2-A6 | 产品化打磨 | `README.md`、`server/**`、`web/**`、`cli.py`、`tests/unit/server/**`、`tests/unit/cli/**` | 契约 v1.2 |

**文件级冲突隔离**（在 Wave 1 包内再细分的两处）：`audit/detect/engine.py` 本轮归 A3；`audit/detect/rules/__init__.py` 与 `audit/detect/registry.py` 归 A4；`cli.py` 归 A6。其余规则文件 A4 只新增不改 T4 已有 `rules/python.py` 等。

## 5. 协作规约（继承 docs/06 五条铁律，Wave 2 补充）

1. 契约文件与"集成人已接线"的 `audit/config.py`、`audit/orchestrator/pipeline.py` **禁止修改**；接口变更走报告申请。
2. 测试零网络、零真实 LLM（FakeLLMClient / httpx MockTransport / monkeypatch）；允许用 git CLI（本机可用）做临时仓库测试。
3. 沙箱、GLM 客户端、索引等 Wave 1 成果**只调用不重写**；发现缺陷记报告。
4. 不得破坏他人存量测试：A3 交付时 `tests/unit/detect` 全绿，A4 同；A6 交付时 `tests/unit/orchestrator` 全绿。
5. 交接物同 docs/06 §2.5：文件清单 / API 签名 / pytest 尾部 / 偏差与变更申请 / 集成备注。

## 6. 真实指标实测流程（W2-周2，用户提供 GLM_API_KEY 后执行）

```bash
export GLM_API_KEY=sk-xxx
python -m bench.run --projects <金标项目目录...> --goldset bench/datasets/goldset.jsonl \
    --ablation --out bench/results/run_$(date +%Y%m%d).md
```

产出：P/R/F1（critical+high）、耗时 P50/P90（s/KLOC）、tokens/KLOC、7 配置消融表 —— 即 docs/04 定义的简历数字证据链。**评测金标 ≥150 条**：demo_proj 12 条（已有）+ 缺陷注入 ≥40 条 + git 历史回溯 ≥60 条 + BugsInPy 抽样（适配器交付，数据集按需下载）。

## 7. 风险与对策

| 风险 | 对策 |
|---|---|
| A1 的 git apply 在非 git 仓库工作副本的兼容性 | 统一 `git apply --check` + `cwd=src_root`（git 支持 repo 外 apply）；失败回退 needs-review，不阻断 |
| A3 改 engine.py 破坏 T4 存量 135 测试 | 可选参数向后兼容 + 交付门槛含 tests/unit/detect 全绿 |
| 修复 Patch 连锁破坏（同文件多 Issue） | A1 顺序应用 + 同文件冲突标记依赖前序；回滚保留原文件内容 |
| 真跑评估成本失控 | config.token_budget 熔断已有；bench 真跑前打印预估，支持 --max-projects 抽样 |
| 单测生成污染用户测试目录 | 只写 `src/tests/generated/`，报告标注可一键删除 |
