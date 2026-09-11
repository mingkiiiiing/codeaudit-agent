# 08 Wave 3 总体方案：收尾、实证与 GitHub 发布

版本：v1.0 ｜ 日期：2026-09-11 ｜ 前置：docs/07（Wave 2 已交付，600 测试全绿，修复/单测闭环跑通）

---

## 1. 现状与 Wave 3 定位

Wave 1/2 已完成全部核心功能。剩余缺口集中在三类：
1. **实证缺口**：消融 7 组中 4 组是占位；金标未扩到 ≥150 条；bench/results 无 run 记录——简历三个数字还差"可复现证据链"（真 LLM 部分等 key，离线部分本轮补齐）。
2. **发布缺口**：还不是 git 仓库、无 LICENSE/CI/演示脚本——不能提交 GitHub。
3. **演示缺口**：面试现场若无网络/key，需要一个离线全闭环演示。

Wave 3 = 收尾周（对应总计划 W6）：**离线实证补齐 → 发布工程化 → GitHub 提交**。真跑评估（GLM_API_KEY）仍属用户侧步骤，不在本轮。

## 2. 目标

| 目标 | 验收 |
|---|---|
| G-A 金标 ≥150 条 + 离线基线 run 记录 | bench/datasets/goldset.jsonl ≥150 条且可加载；bench/results/ 有带日期的离线 run 记录（含 3 组消融 + 纯规则耗时基线），明确标注"LLM 相关指标待真跑" |
| G-B 消融 7 组全部可执行 | ablation.py 零占位；4 个新开关（hints/symbol/cache/llm_only）接线并有测试；600 存量测试零回归 |
| G-C 离线演示 + 发布文档 | `python demo/run_demo.py` 离线全闭环（检测→verified Patch→生成单测→报告）；LICENSE/.env.example/README(mermaid+徽章) 就绪 |
| G-D CI + GitHub 提交 | GitHub Actions workflow（ruff + pytest 矩阵）入仓；本地 git 仓库 3~5 个逻辑提交；推送 GitHub（或给出待执行的两条命令） |

## 3. 契约 v1.3（集成人先行落地，A2 消费）

AuditConfig 新增 4 个字段（全部带默认值，向后兼容）：

```python
enable_rule_hints: bool = True      # false：规则命中不注入 LLM 审查 prompt（消融 −rule_hints）
enable_symbol_context: bool = True  # false：审查上下文不附依赖符号源码（消融 −symbol_context）
enable_llm_cache: bool = True       # false：GlmClient 不做响应缓存（消融 −cache）
llm_only_mode: bool = False         # true：跳过静态规则，纯 LLM 审查（消融 llm_only）
```

## 4. 任务分解表（4 个大任务，全部并行）

| 代号 | 任务名 | 独占目录/文件 | 关键交付 |
|---|---|---|---|
| W3-A1 | 金标扩充与离线评估 | `bench/datasets/**`、`bench/results/**`、`tests/unit/bench/test_goldset_v3*.py` | ≥150 条金标（注入+自建 git 历史挖掘）；离线 run 记录（3 组消融 + 耗时基线）；不生成 LLM 谎称数据 |
| W3-A2 | 消融开关接线 | `audit/detect/engine.py`、`audit/agents/review.py`、`audit/llm/glm_client.py`、`bench/ablation.py`、各自新测试文件 | 4 开关行为接线；ablation 7 组真实化；`tests/unit/detect`、`tests/unit/agents`、`tests/unit/llm` 全绿 |
| W3-A3 | 演示与发布文档 | `demo/**`、`README.md`、`LICENSE`、`.env.example`、`Makefile` | 离线全闭环演示脚本（FakeLLM 脚本化：产出 verified Patch + 通过的单测）；README mermaid 架构图与徽章；MIT LICENSE |
| W3-A4 | CI 与工程质量 | `.github/**`、`pyproject.toml`（仅 [tool.ruff] 节） | GitHub Actions（ruff + pytest，3.11/3.13 矩阵）；ruff 本地零告警（以配置豁免调平，不批量改代码） |

**冲突隔离**：A1 不改 bench/*.py 代码（只加数据与结果文件）；A2 独占 4 个代码文件；A3 不碰 docs/（集成人更新索引）；A4 不碰任何 .py 业务代码。`docs/08` 索引回填与 git 提交由集成人负责。

## 5. GitHub 提交方案（集成人执行）

1. **预检**：全量测试绿；`ruff check` 绿；仓库内 grep 硬编码密钥（`sk-`/`AKIA`/密码字面量）零命中（demo_proj 的假密钥是样例数据，保留但 README 注明）。
2. **初始化**：`git init -b main`；确认 .gitignore 覆盖 `__pycache__/ *.egg-info/ .codeaudit*/ .pytest_cache/`（已覆盖）；`git add` 分批逻辑提交：
   - `docs: 项目设定与开发计划（docs/00~08）`
   - `feat: 审计流水线核心（ingest/index/understand/detect/fix/testgen/report/orchestrator）`
   - `feat: LLM 基建与 Agent 运行时（llm/agent/agents/sandbox）`
   - `feat: 产品化（server/web/cli）与评估基准（bench）`
   - `test+ci: 全量测试与 GitHub Actions`
3. **推送**：远端 `https://github.com/mingkiiiiing/codeaudit-agent.git`。gh CLI 未安装，两条路径：
   - 本机 Git Credential Manager 已存 GitHub 凭据 → 直接 `git push -u origin main`；
   - 无凭据/仓库不存在 → 用户在 GitHub 网页创建**空仓库** `codeaudit-agent` 后执行同一条 push（首次会弹浏览器授权）。
4. **推送后**：确认 Actions CI 首跑绿；README 顶部补仓库 badge。

## 6. 协作规约（继承 docs/06/07，本轮补充）

1. 契约 v1.3 的 4 个字段落地后即冻结；A2 是唯一消费方。
2. A4 对 pyproject.toml 的修改**仅限**追加 [tool.ruff] 配置节，依赖列表零改动。
3. 全部任务交付门槛含全量 pytest（基线 600）无回归 + `ruff check .` 零错误（A4 调平配置后）。
4. 禁网禁真实 LLM 不变；git 仅本地操作（A1 自建历史仓库用）。

## 7. 风险与对策

| 风险 | 对策 |
|---|---|
| 开关接线破坏 600 存量测试 | 全部默认值=True/False 保持现行为；A2 回归门槛最严 |
| 离线 run 记录被误读为真实指标 | 记录头部与 README 明确"offline rules-only baseline，LLM 指标待真跑" |
| ruff 调平诱使批量改代码引入回归 | A4 只允许改配置与豁免，禁止改业务代码 |
| push 需要用户凭据 | 预检凭据 → 可推则推；否则交付"建空仓库 + 一条 push 命令"的最短路径 |
