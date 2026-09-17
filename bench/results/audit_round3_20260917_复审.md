# 第三轮全维度系统性审查报告（W22 后复审，2026-09-17）

- **审计对象**：工作区版本（v0.6.0 + W14~W22 全部未提交改动，pytest 1930 用例）
- **承接关系**：第二轮深度审计（`audit_round2_20260915_深度审计.md`，92/100 整改闭环口径）+ 架构外部审查（59/100，P0 八项）→ W22 六卡清偿后的首轮全维度复审
- **审查方法**：五路并行取证（索引/检测/重构/编排/UX 流程，逐 file:line 证据）+ 上一轮 P1 清偿逐项复核 + 基线复验（pytest 收集 1930 / `make demo` / 抽查载重结论）
- **口径声明**：本轮为「提示词一」外部审查口径（66 项清单逐项判定），与第二轮"整改闭环率"口径不可直接换算；对标基准为 59/100 那轮的同口径框架

---

## 第一部分：总览

| 项 | 值 |
|---|---|
| 目标 Agent | 代码库级智能审计与重构 Agent（CodeAudit），工作区 v0.6.0 + W14~W22 |
| 审查日期 | 2026-09-17 |
| 综合评分 | **66 / 100** |
| 总体评级 | **B**（较上轮同口径 59 分 **+7**，未达 A 线 75） |
| 分维评分 | 功能实现 63（权重 35%）· 用户体验 62（20%）· 流程优化 63（20%）· 系统架构 **78**（25%） |

**一句话结论**：W22 六卡把上轮 P0 全部在实现层清偿（+7 分），工程可靠性与信任边界（架构 78 分）已是同类开源 Agent 的第一梯队；但能力面仍是「Python/JS/TS 三语言 + 行级启发式检测 + 文本补丁修复」——多语言语义索引、数据流级安全、AST 级重构原语、「预览→确认→应用」交互回路四大代际缺口未动，且成本三项（W22-A/B/F）因 GLM 429 余额不足未完成对数、全部保持默认关闭，**综合评分里的成本收益尚未真正兑现**。

### 评分对比（上轮 → 本轮）

| 维度 | 上轮（09-15 前口径） | 本轮 | 变化主因 |
|---|---|---|---|
| 功能实现 | 56 | 63 | +并发/N+1/复杂度规则、命令注入 os.execv 收口、W22-B/F 落地（默认关） |
| 用户体验 | 56 | 62 | W22-C 项目级规则配置 + CLI 旗标、W22-D 部署安全默认 |
| 流程优化 | 60 | 63 | W22-A 消息预算、W22-B 聚焦两级审计（开关在、默认关） |
| 系统架构 | 63 | 78 | 契约层完备性确认、bench 消融/压测体系、任务持久化与灰度防线 |

---

## 第二部分：分维度审查报告

### 1.1 代码库理解与索引 —— ⚠️ 部分具备，6/10

**对标**：SWE-agent（文件级导航 + Tree-sitter 语义解析）/ CodeScene（跨文件依赖与热点分析）；辅对标 Cursor（Merkle 树增量索引）、Aider（repo map 压缩上下文）。**对标水平：落后**（语言覆盖与增量生效度），局部持平（2000 文件级工程闭环）。

**逐项判定**：
- 多语言语义索引：⚠️ 部分。Tree-sitter 真语法解析（非正则）：`audit/indexer/parsers.py:16` `SUPPORTED_LANGUAGES = ("python","javascript","typescript")`；语言识别仅扩展名一档（`audit/utils.py:13-26`）；Java/Go/C++ 构建时直接跳过（`store.py:162-164`）→ **缺失**。
- 调用图/继承/依赖图：⚠️ 部分。`call_edges` SQLite 双索引 + imports 表（`store.py:30-83`），Python import 别名/相对导入/`import *` 解析（`store.py:267-346`）；`call_chain` 深度受限 DFS（`store.py:540-592`）。**继承关系图缺失**（indexer 全目录 grep `inherit/extends/bases` 零命中）。跨文件解析率 resolved_ratio≈**0.178**（`bench/results/stress_20260912.md:99`，2000 文件 9.9 万边）——调用图对动态/跨语言调用基本失效。
- 增量索引：⚠️ 机制在、未接线。文件 sha256 跳过重解析（`store.py:152-172`）真实存在，但每次审计生成新 `audit_id` → 新建 `index.db`（`orchestrator/pipeline.py:334-349`），增量路径实际**永不生效**；call_edges 每次 build 全量 DELETE+重建（`store.py:449`）。
- 大仓库适配：**❌ 缺失**。`max_files=2000 / max_loc=500_000` 超限直接抛错拒绝（`ingest/core.py:104-106`），10 万文件场景被硬上限挡在门外；2000 文件/20.8 万行实测：索引 14.19s、DB 28.6MB、内存峰值 41.7MB（`stress_20260912.md:85-112`），且索引构建零并行、无内存控制手段。
- 生成/第三方/测试/核心区分：⚠️ 部分。索引 schema 无分类列；测试代码区分在检测层（`detect/rules/_python_common.py:174-181`），生成代码仅在报告统计排除（`report/builder.py:15-40`），第三方仅靠目录排除（`workspace.py:13-30`）。

**缺陷清单**：
1. [P0] 语言覆盖 3/7+，Java/Go/C++ 为空 → 触发：索引任何 .java/.go 文件 → 预期：语义符号/调用边 → 实际：`guess_language` 不在白名单直接 continue，该文件对审计不可见。
2. [P1] resolved_ratio 0.178 → 触发：跨文件动态调用/属性链调用 → 预期：可解析边 → 实际：落 `resolved=0`，依赖此图的 W22-F 聚焦测试与后续影响面分析的地基不牢。
3. [P1] 增量索引「有能力未接线」→ 触发：同一项目第二次审计 → 预期：仅重算变更 → 实际：新 index.db 全量重建（2000 文件 14s，10 万文件按比例放大至分钟级）。
4. [P1] 10 万+ 文件不支持：硬上限 2000 文件直接拒绝，无分片/并行索引方案。

**改进方案**（P0 项的完整实现方案见第三部分 P0-3）：
- 增量索引接线：`audit_id` 工作副本保留（副本隔离是安全设计，不动），但 `index.db` 迁到**项目级缓存目录**（`.codeaudit/cache/index-<src_sha_of_fileset>.db`），pipeline 接入时 `build()` 复用既有库做增量解析；call_edges 改为「删除受影响文件的边 + 重解析」，预期重复审计索引耗时降 80%+。工作量：3~5 天。
- 继承图：tree-sitter 捕获 `class_def(superclasses)`/`class_declaration(extends, implements)` 入 `symbols` 新列 `bases`，`call_chain` 之外提供 `hierarchy()` 查询；Type-2 克隆与 LLM 上下文同步受益。工作量：2 天。

### 1.2 审计能力 —— ⚠️ 部分具备，7/10

**对标**：SonarQube（坏味道/复杂度/技术债量化）/ Semgrep（AST + taint 模式规则引擎）；辅对标 Sourcery（Python 质量规则）、CodeRabbit（PR 级增量审查）。**对标水平：工程化持平偏上、分析深度落后**（无数据流）。

**逐项判定**：
- 缺陷检测：⚠️ 部分。规则总量 **82 条注册规则 + 10 个后处理扫描器族**（python 51 / javascript 15 / js+ts 9 / typescript 7）。SQL 注入 ✅（PY-SQL-INJECTION critical，含常量模板单步二级传播 `python.py:812-955`）；硬编码密钥 ✅（熵闸门 Shannon≥3.5 + 字符集≥3 类，`python.py:1285-1358`，报告与 LLM 路径双打码）；资源泄漏 ⚠️（仅 `open()` 一种，`python.py:214-247`；连接/锁/进程句柄缺）；并发竞态 ⚠️（PY-UNSYNCED-SHARED-MUTATION 三条件收敛误报 `py_concurrency.py:55-146`、PY-SLEEP-IN-ASYNC；死锁序/双重检查锁缺）；**空指针解引用 ❌**（无数据流空值分析，仅 TS 非空断言滥用等周边形态）。实现方式：**全部为逐字符掩码 + 正则/缩进启发式，非 AST**——引擎传给规则的 `tree` 恒为 None（`detect/engine.py:153` 实证抽查确认），tree-sitter 只服务索引器。
- 坏味道：✅ 大部分。Type-1/Type-2 克隆（归一化 + K 行滚动哈希，`detect/crossfile.py`）、过长函数 >80 行、死代码（保守口径零误报）、圈复杂度 CC>10（`py_complexity.py:41-42`，上轮 P1 已清偿）、深嵌套/魔法数/TODO。**上帝类 ❌**（静态规则缺，LLM prompt 兜底）；循环依赖在三色 DFS（`refactor/heuristics.py:366-396`）但**产出重构方案而非审计 Issue**，depcheck 名实分离。
- 架构违规：⚠️ 部分。PY/JS-LAYER-VIOLATION 分层穿透（`arch_layers.py:43-48`）白名单**写死不可配**；循环依赖不进 Issue 通道。
- 性能反模式：⚠️ 部分。PY-ORM-N-PLUS-ONE（仅 SQLAlchemy/Django 两形态 `py_orm.py:30-102`）、PY-DEEPCOPY-IN-LOOP、PY-IO-IN-LOOP、PY-SLEEP-IN-ASYNC、PY-NO-TIMEOUT。**大对象全量序列化 ❌**。
- 安全审计：⚠️ 部分。实质覆盖 OWASP 注入类/加密凭据/XSS/PII/日志注入/易受攻击依赖（DEP-CVE 种子库 27 条 + OSV opt-in）；但**无 source→sink 污点传播**（SQL 仅同文件一步传播，docstring 自认多步链/跨行模板不覆盖）；SSRF/越权/认证配置无专规；**OWASP 无显式映射表**。
- 测试覆盖盲区识别：**❌ 完全缺失**（testgen 是"给问题补测试"，不是"识别哪些核心路径无测试"；全库 grep coverage/盲区无检测能力）。
- 分级与误报治理：✅ 工程化高。critical/high/medium/low 四级 + confidence + source 三源合并升置信（`engine.py:469-473`）；critical/high 强制 LLM 复核三裁定（`apply_verify` `engine.py:576-613`），驳回者出终局但留 `verify_stats` 供统计；行内抑制 `# codeaudit: ignore[ID]` + 基线抑制。**但 `verify_stats` 未被 report/render/CLI 消费（本轮 grep 实证零命中）——误报治理数据"记而不显"**，报告读者看不到误报率。

**缺陷清单**：
1. [P0] 检测引擎 AST 断供：`tree=None` 写死 → 触发：任何需要语法结构的规则 → 预期：AST 级判定 → 实际：全部退化为行级启发式，空指针/污点/跨过程分析无地基。
2. [P1] verify_stats 不进报告 → 触发：开启 LLM 复核的任意审计 → 预期：报告展示 checked/confirmed/rejected/uncertain → 实际：仅存 `ctx.extra`，用户不可见。
3. [P1] 循环依赖产出在 refactor 侧 → 触发：纯审计（不开 fix）→ 预期：架构 Issue 呈现 → 实际：仅 RefactorProposal 文本，健康分与门禁均不计。
4. [P2] 分层白名单写死：自定义分层结构的项目全漏。
5. [P2] 上帝类/大对象序列化/SSRF 无规则。

**改进方案**：① 把 tree 传入规则上下文（`RuleContext.tree`，向后兼容默认 None）：tree-sitter 已在依赖中，`engine.py:153` 一处接线 + 规则侧逐条迁移，先迁 SQL 注入与并发两条做灰度；② `verify_stats` 渲染进 md/html 摘要与 JSON（builder.py 一处 + 模板两处，半天）；③ 循环依赖以 architecture 类 Issue 入通道（severity=high，保留 refactor 方案联动）；④ 分层白名单进 `.codeaudit.toml`（W22-C 已有配置骨架，加两键即可）。

### 1.3 重构能力 —— ⚠️ 部分具备（修复管线强、重构引擎弱），5.5/10

**对标**：JetBrains IDE（重构安全边界：预览/引用级联/行为等价）/ Aider（多文件编辑 + git diff 验证）；辅对标 OpenHands、Cursor Composer 多文件编辑。**对标水平：落后**——本质是「LLM 文本补丁修复工具」，重构原语缺位。

**逐项判定**：
- 自动化重构操作：**❌ 语义级原语全缺**。无 rename/extract-function/extract-class/inline/move-file/change-signature 的 AST 实现。fix 类 = LLM 生成 unified diff（prompt 硬性要求"最小改动不顺手重构"，`fix/patcher.py:51-62`）；refactor 类 = 4 类启发式方案（decompose/dedup/split-module/依赖环，`refactor/heuristics.py:539-553`）经 W15-E 闭环注入 fix 管线（`refactor/stage.py:67-109`），最终执行的仍是文本 diff。
- 行为等价闭环：✅。generate → `validate_diff`（路径安全）→ `git apply --check` → apply → 语法双保险（tree-sitter + `ast.parse`，`fix/verifier.py:42-69`）→ 沙箱测试回绿才 verified；沙箱白名单仅 pytest/jest/node-test，可选 Docker 后端（`--network none --memory 512m --cpus 1`，`sandbox/executor.py:317-348`）。
- 大规模跨文件：**❌**。多文件 diff 机制可行（`_diff_targets` 取 ---/+++ 并集）但 fix prompt 按单文件上下文设计；接口变更级联**只检测不修复**（`fix/compat.py:278-308` compat_notes 仅提示）。
- 粒度控制：⚠️。逐 Issue 顺序处理、每 patch 独立验证回滚 + 字节级备份（`stage.py:186-210`）+ 根本隔离（只写 `.codeaudit/<id>/src` 工作副本）；但**无 per-patch commit/分支分层**，"分步骤可回退的小步提交"只做到回退、没做到提交。
- 预览模式：⚠️。`--fix`/`--tests` 默认关（opt-in 即"不确认就不动"），diff 进报告 + Web DiffView 可视；**无「展示计划→用户确认→系统应用」前置回路，也无 apply-back-to-source 端点**——用户拿到 diff 文本自行 `git apply`，闭环断头。
- 自动更新测试/文档/调用方：⚠️/❌。testgen 为 verified hunk 相交符号生成全新单测（静态硬闸门 → 沙箱实测 → 失败剔除，质量链完整）但只写 `tests/generated/`；**文档与调用方零更新**。

**缺陷清单**：
1. [P0] 无 AST 重构原语 → 触发：重命名一个被 12 处引用的函数 → 预期：一次级联 12 处 → 实际：无此操作类型；LLM 文本 diff 只会改单文件单点。
2. [P0] apply-to-source 断头路 → 触发：用户认可某个 verified patch → 预期：一键应用回源码树 → 实际：手工复制 diff 文本自行 git apply。
3. [P1] 无测试时行为等价验证形同虚设 → 触发：被改函数无既有测试 → 实际：降级 syntax-ok 即放行（诚实但等价性未证）。
4. [P2] commit message/PR 描述缺失。

**改进方案**：语义原语分两步——第一步「索引驱动文本化 rename」（`store.py` symbols 全库定位定义+引用行，逐行文本替换 + 语法重解析验证 + 聚焦测试，覆盖 80% 场景，工程量 3~4 天）；第二步 tree-sitter AST 改写（Tree-sitter edit + 重建文本，成本高，随 P0-4 检测 AST 化一并建设）。apply-to-source 见 P0-2。

### 1.4 变更与验证 —— ⚠️ 部分具备，6.5/10

**对标**：Aider（编辑后自动测试回环）/ SWE-agent（测试驱动验证）；辅对标 OpenHands。**对标水平：验证闭环持平，lint/commit 落后**。

- 受影响测试选择：✅ W22-F 已落地。`_affected_test_target`（`fix/stage.py:149-183`）经调用图 callers 收集直接调用方中的测试文件，聚焦单文件运行；pytest exit 5 自动整库兜底（`:449-456`）；`fix_stats.focused_tests_runs` 计数。边界诚实：动态调用解析不到回退整库。
- Lint/类型检查/编译验证自动触发：⚠️。仅语法重解析（tree-sitter + ast.parse）；**无 ruff/mypy/pyright/eslint/tsc 集成**——「类型检查」项缺失。
- 失败自动回滚：✅。字节备份 + 异常兜底回滚 + 副本隔离双保险。
- 变更摘要（Commit Message 建议）：**❌**。全库 grep "commit" 仅 SQLite 事务命中；最接近的是 `Patch.rationale`（中文修复思路，进报告）。

**改进方案**：① 验证链插入「工具链检测」步：目标项目存在 ruff/mypy/eslint 配置文件即调用其 CLI（存在才用，不存在跳过，输出并入 verified 判定旁路信息，不做硬门禁——避免误杀），工程量 2 天；② commit message：由单 patch 的 issue 描述 + diff 生成 conventional commit 一行文（纯模板拼接即可不耗 token：`fix(<category>): <issue title> (<rule_id>)`），批量场景按文件聚合，1 天。

### 2.1 交互设计 —— ⚠️ 部分具备，5.5/10

**对标**：Cursor Composer（@ 提及 + 自然语言混合输入）/ Cline（进度条 + 步骤展开 + checkpoint）。**对标水平：落后**。

- 任务入口：⚠️ 纯参数驱动。CLI 四子命令 run/index/report/serve（`cli.py:384-487`），API 仅 `{source_path, do_fix, do_tests}`（`server/app.py:343-348`）；无自然语言需求、无 @ 提及；PR 机器人在 Roadmap 未实现（README:305）。
- 进度可视化：⚠️ Web 有、粒度粗。SSE 事件流（seq 游标回放 + 断流降级轮询，`app.py:682-714`）+ 七阶段 Steps + 当前事件行（ProgressPanel.tsx:37-127）；事件协议支持 `current/total` 且 fix 阶段真实发出 `[3/12]`（`stage.py:313-321`），**但前端不渲染百分比进度条、无 ETA**；**CLI 运行中零输出**（事件只进 list 用于降级判断，`cli.py:179-198`）——低成本高收益的改进点。
- 中途干预：⚠️ 仅取消。Web 删除任务 = 协作式取消（`app.py:351-364`，事件边界检查哨兵）；无暂停/恢复/跳过某文件/运行中改指令。
- 结果展示：✅。report.json/md/html 三格式 + SARIF；Web 仪表盘健康分 gauge + 服务端 severity/category 过滤 + 关键词过滤 + 行展开详情；缺文件跳转编辑器/仓库链接。
- Diff 展示：⚠️。unified diff 逐行着色（DiffView.tsx:13-62），非 side-by-side；**无逐 hunk 接受/拒绝**（纯查看）。

**改进方案**：CLI 实时进度（事件回调里按阶段打印 `[stage 3/7] 当前文件 (n/m)`，`cmd_run` 已收集事件、只差消费，半天）；前端 progress 条（`current/total` 已在事件协议中，1 天）；逐 patch 接受/拒绝随 P0-2 apply 端点一并做。

### 2.2 信任与可控 —— ✅ 已具备，8/10（全项目最强 UX 项）

**对标**：Cline（checkpoint 回滚 + 明确的自动/确认边界）/ CodeRabbit（可追溯评论）。**对标水平：领先于同类开源 Agent 项目**。

- 自动 vs 确认边界：✅ `--fix/--tests` opt-in 显式声明；补丁只写隔离工作副本、绝不触碰用户源码（`ingest/core.py:4`），失败自动回滚。
- 危险操作强制防线：✅ W22-D 非回环绑定 + 未设 token + 未传 `--allow-insecure` → 中文报错退出 1（`cli.py:312-325`）；删除任务 Popconfirm 二次确认；source_path 白名单越界 400（`app.py:253-272`）。
- 只读/重构模式开关：✅ `--no-llm` 纯规则离线 + `--fix/--tests/--no-verify` 组合。
- 可追溯：✅ SQLite TaskStore 全量事件流（seq 游标可回放）、config_json 落库前 api_key 脱敏（`app.py:440-450`）、stage_errors/fix_stats/budget 熔断全落 `ctx.extra`。

**缺陷清单**：1. [P2] 直接 `uvicorn server.app:app` 绕过 W22-D CLI 防线（文档已知边界，README 应给出指引）；2. [P2] `--allow-insecure` 旗标名偏"同意书"化，无风险提示打印。

### 2.3 学习与适配 —— ⚠️ 部分具备，5/10

**对标**：CodeRabbit（learnings：从用户反馈学习偏好，减少重复建议）/ Continue.dev、`.cursorrules`（项目级规则）。**对标水平：规则配置持平、偏好学习缺失**。

- 项目级规则配置：✅。`.codeaudit.toml` / `pyproject [tool.codeaudit]`，发现顺序 CWD 优先回退项目根（`cli.py:45-59`），优先级 默认<文件<env<CLI；W22-C 三键（disabled_rules/severity_overrides/ignore_paths）+ `--disable-rule/--ignore-path` 全接线，未知规则/非法严重度记 warnings 不阻断。
- 偏好学习：**❌**。无接受/拒绝历史学习机制（grep 零命中），Roadmap 亦无规划。CodeRabbit 的 learnings 是逐 PR 反馈沉淀到组织记忆——本项目的对应物应该是「基线抑制 + 行内 ignore + rejected 样本回喂 verify 阈值」的组合，目前只有前两者。
- 首次引导：⚠️。`make demo` 10 秒离线闭环 + `--version` + `.env.example` + serve 启动横幅；无 `doctor`（环境自检：Key 有效性/沙箱可用性/tree-sitter 语言包）与 `init`（生成本项目 .codeaudit.toml 骨架）。

**改进方案**：`codeaudit doctor`（零 Key 可跑：检查 Python 版本/git/沙箱后端/语言包/.env 白名单键/配置文件合法性，输出中文诊断表）+ `codeaudit init`（写 .codeaudit.toml 注释模板），合计 1~2 天；偏好学习列长期能力（rejected 样本本地沉淀 → verify prompt 注入"本项目已驳回模式"清单），随 P0-7 模型路由后的成本下降再启动。

### 3.1 审计流程 —— ✅ 已具备（框架），7/10

**对标**：CodeRabbit（PR 增量审查 + CI 自动触发）/ SonarQube（分层质量门禁）。**对标水平：CI 集成持平偏上、在线成本落后**。

- 分层流程：✅ 框架在。W22-B 风险聚焦两级审计（`llm_review_top_files`，风险分 = 规则命中按 SEVERITY_WEIGHT 加权 + 无命中文件行数垫底 + 字典序 tie-break 保确定性，`engine.py:619-650`）——**默认 0=全量，判据未验**；`--diff <ref>` 增量只审变更文件 + 基线抑制存量豁免。
- 并行化：⚠️ 有限。规则扫描 `rule_scan_workers` 可配（默认 1；W7 实测并行反而慢 43% 故默认关）、LLM 文件级并发 Semaphore(8)；索引/克隆/死代码全串行。
- 增量审计：✅。diff 剪枝 + baseline 指纹（sha1）。
- CI/CD 集成：✅。`--check/--fail-on` 退出码 3 门禁（stdout 保纯净供 CI json.loads）、SARIF 2.1.0 + partialFingerprints、自带 `pr-audit.yml`（自审计 + SARIF artifact + concurrency 取消旧跑）、README upload-sarif 示例；无 pre-commit 钩子。

**缺陷清单**：1. [P0] 聚焦两级审计默认关 + 对数未跑（GLM 429）→ 在线 1398 s/KLOC 的成本根因**形式上已修、实质上未生效**；0.884 基线属 prompt v2 口径，v3 复跑前不可外引。2. [P2] rule_scan_workers 无 CLI 旗标。3. [P2] 无 pre-commit 集成。

### 3.2 重构流程 —— ⚠️ 部分具备，5.5/10

**对标**：Copilot Workspace（计划→执行→验证工作流）/ Aider（多文件 + git diff 提交粒度）。**对标水平：落后**。

- 标准流水线：✅ 七阶段（ingest→index→understand→detect→refactor→fix→testgen→report）+ fix 内部微流水线（生成→校验→应用→验证→回滚）。
- 结构化方案：✅ RefactorProposal（rationale/steps/priority P0-P2/estimated_effort_hours）。
- 拓扑排序执行：**❌** 逐 Issue 顺序处理，无依赖序。
- 重构批次：**❌** 无"拆多个可独立提交的小批次"机制（`fix_max_patches=50` 是数量上限不是批次）。
- PR/MR 描述：**❌** 零实现。

**改进方案**：批次化按「文件 → 依赖拓扑序（imports 图已在索引）」分组：同文件 patches 串行、跨文件按被依赖者优先，每批结束快照备份点 + 汇总事件（工程量 2~3 天）；PR 描述随 P0-8。

### 3.3 效率优化 —— ⚠️ 部分具备，6.5/10

**对标**：Aider（repo map 压缩上下文）/ Cline（上下文窗口管理）。**对标水平：机制持平、实效未兑现**。

- Token/成本控制：⚠️ 机制齐全——400 行切片 + 截断标记、>400 行按函数分组逐片审查、批量小切片 5 文件/组合并一次调用（`review.py:722-743`）、W22-A 工具循环消息折叠（60k 字符预算，只折叠 tool 消息保配对，`runtime.py:43-80`）、响应缓存（sha256 key，FIFO 4096）、token 计量 + 全局预算熔断闸门（`pipeline.py:86-186`）。**但 LLM 通道默认仍全量逐文件**（W22-B 默认 0），在线成本根因的实际缓解待对数。
- 重复分析复用：⚠️。LLM 缓存是**进程内 dict 不跨运行**；索引每任务新建（同 1.1）。
- 上下文窗口管理：✅。切片/折叠/架构卡片（map-reduce）承担 repo map 职能。
- 快/深两档：⚠️ 隐式。`--no-llm` 快档、`--review-mode simple|tools` 深档、聚焦档——能力在，未形成 UX 概念与文档化推荐组合。

**改进方案**：缓存落盘（`.codeaudit/cache/llm/<sha>.json`，TTL 按文件 sha256 关联，重复审计同文件免重审，与 P0-6 resume 共用基建）；「fast/balanced/deep」三档作为文档化预设组合（映射现有旗标，零新代码）。

### 4.1 整体架构 —— ✅ 已具备，8/10

**对标**：OpenHands（Agent 架构分层）/ CodeScene（解耦度量，以其标准衡量本项目自身）。**对标水平：持平偏上**。

- 模块化：✅ 五个契约层（LLMClient `llm/base.py:55-76`、AgentRuntime/ToolSpec/AgentLimits `agent/base.py:27-82`、IndexStore 12 抽象方法 `indexer/base.py:11-65`、Rule/RuleRegistry `detect/base.py:72`、PipelineContext `pipeline.py:26`）+ 统一 `AuditError` 异常族；orchestrator 对子包延迟导入 + ImportError 降级。
- 接口替换：✅。LLM/沙箱/索引均可替换实现（FakeLLM 脚本回放、subprocess/Docker 双沙箱后端是活证）。
- 插件化：⚠️。自定义规则 ✅（`register_rule` 公开 API，上轮实测可用）；**自定义重构策略 ❌**（无注册口）；post-scan 扫描器是硬编码模块元组（`engine.py:515-519`）。
- 单体/分布式：单体 + `--workers N` 多 worker（实验特性）+ 全局 429 准入 + per-worker 信号量；**索引层无横向扩展方案**。

**缺陷清单**：1. [P1] detect↔agents 逻辑循环引用靠函数内延迟导入规避（`engine.py:825/871` ↔ `review.py:564`）——能跑但是债；2. [P2] 重构策略无插件口；3. [P2] 扫描器清单硬编码。

### 4.2 LLM 编排 —— ⚠️ 部分具备，7.5/10

**对标**：OpenHands（多 Agent 协作 + 事件流）/ LiteLLM·RouteLLM（模型路由与 fallback）。**对标水平：结构化输出持平、路由/fallback 落后**。

- 多 Agent 协作：⚠️。Review（三路径产出）+ Verify（critical/high 复核三裁定）双 LLM 角色生成-复核协作；understand/refactor/fix 为启发式底座 + LLM 增强（非独立 Agent 角色）；**静态七阶段 pipeline，无顶层动态规划/ReAct 调度器**——对本项目"批量审计"场景，静态管线的确定性反而是优点，判定为合理取舍而非缺陷。
- Prompt 版本管理 + A/B：✅。PROMPT_VERSION=v3 集中定义（`agents/prompts.py:20-96`）+ 版本号进 `ctx.extra` + 8 组消融配置全部映射真实开关（`bench/ablation.py:30-47`）+ mini_bench 回归（P≥0.6/R≥0.5）。
- 模型路由：**❌**。单 provider（GLM OpenAI 兼容）、单模型全流程；`chat()` 无 per-call model 参数（`glm_client.py:140-145`）。
- Fallback：⚠️。同 provider 指数退避 + 429/5xx/TransportError 重试完备（`glm_client.py:133-184`）；**跨 provider/跨模型 fallback 无**——W22-E 对数因余额不足整轮失败即是单点故障的实证。
- 结构化约束：✅。Function calling JSON Schema（10 工具逐字段）+ json_mode 双轨 + `validate_issue_payload` 行号越界拒绝 + 解析失败确定性降级（review 宁缺毋滥/verify 封顶 0.5/refactor 降回启发式/未收口显式 RuntimeError）。未用 JSON Schema 约束 response_format（json_object 无 schema），可接受。

**改进方案**：`AuditConfig.model_routing = {"review": "glm-5.3-flash", "verify": "glm-5.3", "fix": "glm-5.3"}` + `LLMClient.chat(model: str|None)` 增量参数 + fallback 链 `["glm-5.3-flash","glm-5.3"]`（429 连续 N 次降档并记 `extra["llm_fallback"]`）——批量审查用 flash、复核与补丁用强模型，成本与质量分治。工程量 2~3 天，验收：消融第 9 组 routing 对比 Precision 降幅 ≤1pp 且成本降 ≥30%。

### 4.3 数据与状态 —— ⚠️ 部分具备，7/10

**对标**：CodeScene（历史对比/趋势）/ Cline（任务恢复 checkpoint）。**对标水平：持久化持平、恢复与对比落后**。

- 存储方案：✅ 合理。索引 SQLite 6 表（files/symbols/imports/calls/call_edges/slices，2000 文件 28.6MB）；任务元数据独立 SQLite（WAL + busy_timeout + 写锁退避 + BEGIN IMMEDIATE 分配事件 seq）。
- 任务持久化/断点恢复：⚠️。任务/事件/报告全持久化、SSE seq 游标回放 ✅；**无 resume**：重启 sweep 把遗留 queued/running 置 failed（`taskstore.py:489-531`），断点只能从头再跑。协作式取消哨兵 + 容量淘汰 + 磁盘回收完备。
- 结果版本化/对比：⚠️。按 audit_id 可回查历史 + 基线指纹抑制 + SARIF partialFingerprints 跨次去重；**无内置"两次报告 diff 报表"**（新增/修复/回归三栏）。
- 敏感信息：✅。api_key 落库脱敏、密钥类规则报告与 LLM 路径双打码（`review.py:449-460`）、上传路径白名单。

**改进方案**：resume MVP——以 stage 为粒度持久化 `stage_done` 清单到 taskstore（每阶段完成即写事件已有），重启恢复时跳过已完成阶段（索引阶段复用 1.1 的项目级缓存库），工程量 3~4 天；报告对比 `codeaudit diff <id1> <id2>`（issues 按 (file,rule,±3 行) 匹配出 fixed/new/persisted 三栏，2 天）。

### 4.4 可靠性与工程质量 —— ✅ 已具备，8.5/10（全项目最强项）

**对标**：Aider/Cline 的可靠性实践 + 常规开源工程标准（覆盖率 80% 线）。**对标水平：领先同类开源 Agent 项目**。

- 错误处理与重试：✅。阶段级"审计永不整体失败"（stage_errors 记账）、LLM 指数退避、SQLite 锁两级缓冲、沙箱超时击杀 + 输出限量排空 + 容器清理、CLI 门禁退出码体系。
- 监控指标：⚠️。业务统计齐（token/耗时/缓存命中/健康分/verify 四态/fix 六桶+聚焦计数）；**无 /metrics 端点、无外部指标系统**。
- 测试：✅。本轮实测收集 **1930 用例**（134 单测文件 1826 函数 + 33 集成 + 1 property；单测:集成 ≈55:1 偏科但关键路径有）；FakeLLM 契约测试贯穿。
- 性能基准：✅ 稀有亮点。bench 包体系完整（金标 precision/recall/F1 ±3 行容差、8 组消融、mini_bench 回归、stress 2000 文件固化基线、soak/adversarial/canary/memdiag、W22 focus 对比脚本）。
- 配置管理：✅ 四级优先级文档化 + 未知键告警。
- 日志：⚠️。progress 事件流是主链路（audit_id 贯穿）但 **logging 使用稀疏、无统一分级配置、run_id 未注入日志上下文**。

**缺陷清单**：1. [P1] 无 /metrics 与结构化日志 → 长期运营可观测性靠事件流单腿走路；2. [P2] 集成测试比例 1:55。

---

## 第三部分：P0 缺失项清单（按实施顺序）

| # | 缺失项 | 为什么是 P0 | 依赖 | MVP 验收标准 |
|---|---|---|---|---|
| P0-1 | **成本三项翻默认收尾（W22-A/B/F）** | 在线 1398 s/KLOC 是上轮 P0 之首；六卡已写完但 A/B 默认关、对数未跑，收益=0。不做则 0.7.0 打版带的是"未验证的开关" | GLM 充值（外部阻塞）；`bench/w22_focus_compare` 已就绪 | 对数跑通：P50 ≤ 300 s/KLOC 且 Precision 降幅 ≤ 3pp → 翻 `llm_review_top_files` 默认值并回填 docs/21 §5；不达标记录原因维持关闭 |
| P0-2 | **预览→确认→应用回路（apply-to-source）** | 重构闭环目前断头在"用户手工 git apply"，这是 Agent 定位与工具定位的分水岭；也是 UX 权重项 | 无（纯增量） | `POST /api/audits/{id}/patches/{n}/apply` + CLI `codeaudit apply <audit_id> [--patch N] [--all-verified]`：默认 dry-run 输出将写入的用户文件清单，`--yes` 才落盘；目标文件 sha256 与审计时不一致则拒绝（防覆盖用户新改动）；verified 才允许 `--all`；+10 用例含 sha256 漂移拒绝 |
| P0-3 | **AST 断供修复 + 空值/轻量污点** | 82 条规则全部行级启发式，空指针/污点/跨过程为 0；tree-sitter 已在依赖中，`engine.py:153` `tree=None` 是一处接线的事——投入产出比全场最高 | 无 | `RuleContext.tree` 可选字段（默认 None 向后兼容）；先迁移 SQL 注入/并发/ORM-N+1 三条规则到 AST 判定；新增 intra-file 空值流规则 PY-NONE-DEREF（赋值 None → 同函数内解引用属性/下标）：5 例语料 ≥4 检出、clean 语料误报 0；金标全量不回退（P/R 各 -1pp 以内） |
| P0-4 | **Java 语言支持（Go 随后）** | 简历/赛题口径"多语言"当前 3/7，Java 是审计需求最大生态；tree-sitter 语言包成熟 | P0-3 的 tree 接线复用 | `tree-sitter-java` 接入 parsers + `java_extract`（类/方法/字段符号、import、调用点）；语料 10 文件 resolved_ratio ≥ 0.4；3 条移植规则（JAVA-SQL-INJECTION / JAVA-HARDCODED-SECRET / JAVA-LONG-FUNCTION）金标命中 ≥90%；content-based 语言识别（扩展名 + shebang/package 声明双档） |
| P0-5 | **测试覆盖盲区识别** | 上轮 P1 遗留，数据已全在索引（symbols×call_edges×is_test_file），是"低工程量高故事性"项；审计报告没有它就不算"审到测试" | 无（依赖索引质量，随 P0-3/P0-4 改善） | 报告新增 `untested_hotspots` 节：critical/high Issue 所在的非测试文件中，其公开符号经 callers 反查零测试文件触达的清单（TOP 20 + 占比）；demo 项目上输出非空且人工抽验合理 |
| P0-6 | **resume 断点续跑 + 报告对比** | 长任务（大项目在线审计）中断=全损，是"任务状态持久化"清单的直接缺口；两报告对比是审计产品的基本盘 | 1.1 的项目级索引缓存（可拆先后） | 杀进程后重启同任务：已完成阶段不重跑（事件流可验证），总耗时 < 全量的 50%；`codeaudit diff <id1> <id2>` 输出 fixed/new/persisted 三栏且与手工比对一致 |
| P0-7 | **模型路由 + fallback** | 429 余额故障整轮失败已发生（W22-E 实证），单 provider 单模型是可用性单点；也是成本分治的前提 | 无 | `model_routing` 配置三角色分模型；连续 3 次 429 自动降档 fallback 并记 `extra["llm_fallback"]`；消融对比 Precision 降幅 ≤1pp |
| P0-8 | **commit message / PR 描述生成** | 1.4/3.2 两维度共同缺口，模板拼接即可零 token 实现，不做则"重构后自动生成 PR"链路无法讲 | P0-2（apply 后才有 commit 语义） | 每个 verified patch 产出 `fix(<category>): <title> (<rule_id>)`；`--all-verified` 批量应用后输出聚合 PR 描述（含 issue 清单与测试验证结果） |

**依赖关系图**：P0-1 独立（唯一外部阻塞=充值）→ P0-2/P0-5/P0-7 独立可并行 → P0-3 先行、P0-4 跟随 → P0-6 前半独立/后半依赖索引缓存 → P0-8 依赖 P0-2。

---

## 第四部分：重构路线图

### 短期（1-2 周）：兑现 W22 + 闭环断头路
1. GLM 充值 → `w22_focus_compare` 对数 → 判据判定 → 翻/不翻默认并回填 docs/21 §5（P0-1）。
2. P0-2 apply-to-source（CLI + API + sha256 防覆盖）。
3. 快赢三件：`verify_stats` 进报告（半天）、CLI 实时进度（半天）、`codeaudit doctor/init`（1~2 天）。
4. **验收指标**：全量 pytest ≥1940 绿 + demo 离线闭环不回归 + `--dry-run` 退出码 0 +（若对数达标）focus 模式 P50 ≤ 300 s/KLOC。

### 中期（1-2 月）：能力面扩张
1. P0-3 AST 接线 + 三规则迁移 + PY-NONE-DEREF；P0-4 Java 语言包 + 3 规则；Go 视 Java 验收顺延。
2. P0-5 测试盲区报告、P0-6 resume + 报告对比、P0-7 模型路由 + fallback、P0-8 commit/PR 描述。
3. 架构债清偿：detect↔agents 循环引用收敛（verify_fn/review_fn 全部走 orchestrator 注入）、分层白名单配置化、扫描器注册表化。
4. **验收指标**：Java 语料 resolved_ratio ≥ 0.4；空值流 5 例 ≥4 检出误报 0；金标 P/R 不回退超 1pp；路由消融成本降 ≥30%；新增强化用例 ≥120。

### 长期（3-6 月）：代际建设
1. 跨文件级联重构原语（签名变更 → callers 全量级联 + 每步验证，对标 JetBrains 重构安全边界）。
2. 跨文件污点传播（taint）引擎——LLM tools 通道做混合取证，专项基准先行（P≥0.6）。
3. 偏好学习（rejected 样本沉淀 → verify/审查 prompt 注入项目记忆，对标 CodeRabbit learnings）。
4. 大仓库：2000 文件上限放开 → 并行索引 + 项目级增量缓存（Merkle 式）+ 10 万文件/百万行压测基线。
5. PR bot / IDE 插件形态（Roadmap 已列）。
6. **验收指标**：接口变更级联测试 12 处引用全更新且测试绿；taint 基准 P≥0.6；10 万文件索引 ≤30min；PR bot 在真实仓库完成 10 个 PR 审查闭环。

---

## 第五部分：风险提示

**技术债**：
1. detect↔agents 循环引用靠延迟导入维持——每加一个交叉点都会恶化，建议 0.7.0 前收敛。
2. call_edges 每次 build 全量 DELETE+重建 + per-audit 独立 index.db——规模一大（10 万文件）就是分钟级固定成本，且 resolved_ratio 0.178 意味着上层功能（聚焦测试、盲区识别、影响面）建立在解析率不足两成的图上，**索引质量是所有下游能力的公共放大器**。
3. 契约文件"勿改"纪律出现第一个报备破例（W22-A 对 `agent/base.py` 增量字段）——本次处理规范（默认值零变化 + 报备），但要防止破例惯例化。
4. W7 实测规则扫描并行反而慢 43% 而默认串行——诚实，但意味着"多模块并行分析"清单项实际无并行红利，未来需按文件分片重测而非全局开关。

**安全风险**：
1. **代码出域**：被审代码全文进入 GLM 云端（LLM 审查/补丁/测试生成都含源码切片），无私有化部署路径说明——企业落地必须给出「数据流说明 + 可选脱敏/本地模型接口」（`LLMClient` 契约已抽象，替换实现可行）。
2. **沙箱隔离强度**：默认 subprocess 后端在用户权限下执行 LLM 生成的测试代码（Windows 上仅进程组隔离），Docker 后端可选但非默认——README 应明示风险并推荐 CI 内使用 Docker 后端。
3. SARIF 上传 GitHub 会携带代码片段出域；直接 `uvicorn` 启动绕过 W22-D 防线（文档已知，需部署指引收口）。

**性能瓶颈预判**：LLM 全量通道（默认）×大文件多 → 在线成本线性爆炸（已有 1398 s/KLOC 实证）；克隆检测 30 万行护栏超限整体跳过（记 post_scan_errors 但报告不醒目，**静默降级风险**——建议汇总事件显式告警）；`imported_by` 全表扫描 O(n)（W22 设计决策已记录 MVP 不做度数，长盯）。

**合规风险**：CVE 种子库 27 条为手工核实快照，**时效性衰减**（建议 CI 每季刷新 + OSV opt-in 文档强化）；依赖许可证本体（tree-sitter 语言包 Apache/MIT）兼容；LLM 生成测试/补丁的版权与许可证污染风险未在文档声明；代码出域涉及客户合同/数据隐私条款时需部署方自行评估（与安全风险 1 同源）。

---

## 附：本轮审查可复现性（实测记录，2026-09-17）

- 全量测试：`python -m pytest -q` → **1930 passed, 5 warnings in 819.70s（13 分 39 秒），退出码 0**（本轮实测；5 条 warning 均为 bench 金标加载器的预期告警）。
- 离线闭环：`python demo/run_demo.py`（Makefile `make demo` 等价命令；本机 Git Bash 无 make，直跑原命令）→ 6 步全跑通：Patch 状态 verified、生成单测沙箱重放 5 passed（exit_code=0）、三格式报告落盘、总耗时 34.9s、退出码 0。
- 测试基线：`python -m pytest --collect-only -q` → 1930 collected。
- 载重结论抽查：`grep -n "execv" audit/detect/rules/python.py`（上一轮 P1 已清偿实证）、`audit/detect/engine.py:153` tree=None、`grep -rn verify_stats audit/report/ cli.py` 零命中。
- 离线闭环：`make demo`（本轮实测，结果见审查记录）。
- 证据坐标全部来自五路并行取证的实读（索引/检测/重构/编排/UX 五份原始记录在案），行号对应当前工作区版本。
