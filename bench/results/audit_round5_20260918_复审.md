# 第五轮全维系统性审计报告（2026-09-18）

> 审计对象：代码库级智能审计与重构 Agent（CodeAudit Agent）
> 审计轮次：第五轮（工作流路由：上轮=工作 W24-E → 本轮=审计）
> 审计基线：bb471c0（W14~W24 已提交）+ 工作区 W24-E 改动（10 文件，未提交）
> 上轮对照：audit_round4_20260917_复审.md（70/100 B）

---

## 第一部分：总览

- **目标 Agent**：CodeAudit Agent（D:\Project\代码库级智能审计与重构 Agent，GitHub mingkiiiiing/codeaudit-agent）
- **版本状态**：v0.6.0 已打版；bb471c0（W14~W24 十一波累积，211 文件）已提交未 push；W24-E（resume 扩展）在工作区待提交
- **审查日期**：2026-09-18
- **综合评分：71 / 100（B）**，较第四轮 70 分 **+1**
  - 功能实现 70（权重 35%）｜ 用户体验 66（20%）｜ 流程优化 68（20%）｜ 系统架构 79（25%）
- **总体评级：B**
- **一句话结论**：W24 三卡与 W24-E resume 扩展全部实证站住（金标双门逐位一致、Java E2E 逐位复现、全量 2152 绿），P0 八项已清偿六项，剩余 P0-1/P0-7 双双被本轮 429 实探再次钉死在"在线成本与单点路由"上——规则通道已达金标满分，在线通道的对数判据与治理仍被外部配额卡死；另新发现 2 个工程可复现性/状态机边界缺陷（bench.run 口径坑、CLI resume 无 sweep 前置），均低成本可修。

### 本轮实测数据（全部现场复跑，非引用历史）

| 验证项 | 结果 | 对照基线 |
|---|---|---|
| 全量 pytest | **2152 passed, 2 skipped**（350.20s） | W24-E 申报 2140+12 ✅ 逐位一致 |
| ruff check audit cli.py server | All checks passed | 绿 ✅ |
| demo 端到端闭环 | verified，12.2s | verified ✅ |
| 金标双门（240 标签/10 项目，离线强制） | **P=1.0 / R=0.8444 / F1=0.9157**；counts 240/90/76/74/74 | w24 收口 1.000/0.844/0.916 逐位一致 ✅ 零回退门成立 |
| Java E2E（13 文件语料） | **8 命中**（密钥×4/SQL注入×3/长函数×1）+ 8 反例文件零误报 | docs/23 收口记录逐位一致 ✅ |
| Java AST 解析率 | 13/13 = 100% | ✅ |
| codeaudit doctor | 8 OK / 1 警告（Docker 缺失如实降级）/ 0 失败 | ✅ |
| GLM 在线通道单点探针 | **HTTP 429**「余额不足或无可用资源包」 | P0-1/7 对数继续阻塞（429=套餐限流定性不变） |
| 72h soak 巡检（automation-e1bd3b2d） | 27/36 全 PASS（最新 09-18 22:12） | 预计 09-19 收口 |
| 每小时空转自动化（automation-d6351d95） | **已停用**（enabled=false, paused, runCount=6） | 上轮建议已被用户采纳 ✅ |

---

## 第二部分：分维度审查报告

### 一、功能实现（权重 35%）—— 70/100

#### 1.1 代码库理解与索引 —— 评分 7.5/10（持平）
- **状态**：⚠️ 部分具备（4/7 语言）
- **对标**：SWE-agent（文件级导航+Tree-sitter）/ CodeScene（依赖分析）
- **现状**：tree-sitter 语义解析 python/js/ts/java 四语言（`audit/indexer/java_extract.py` 本轮深审：三形态 import、嵌套类 FQN `Outer.Inner`、`Class.method` 限定、全大写字段按常量口径、lambda/注解内调用不做且 docstring 明示边界）；调用图/继承/依赖图有 py/js/java 三语言提取器支撑；跨文件克隆（Type-2）、死代码、架构分层违规（arch_layers）已具备。增量索引以工作副本+index.db 复用形态部分具备（resume 跳过 ingest/index）。49 万行实测 0.278 s/KLOC（W19 总装），大仓库内存峰值 218MB 可控。生成代码/第三方/测试代码区分：部分（lock 文件解析、测试目录识别有；生成代码区分无）。
- **缺陷**：Go/C++ 缺失（Go 已列工作轮候选③，Java 验收达标构成跟随条件）；泛型/注解内 Java 调用点不做（首轮口径，语料对齐式达标——语料外真实仓库 resolved_ratio 会低于 0.517，属**口径性风险**需在 README 标注）。
- **本轮证据**：tests/corpus/java 13/13 AST 解析成功；`java_extract.py:113-129` 调用点收集、`:141-159` import 三形态。

#### 1.2 审计能力 —— 评分 7.5/10（持平）
- **状态**：✅ 已具备（规则通道）/ ⚠️ 部分具备（数据流级）
- **对标**：CodeRabbit（规则引擎+PR 审查）/ Sourcery（坏味道）
- **现状**：86 条规则（rules.md，含 JAVA 三条）覆盖注入/密钥/并发/N+1/复杂度/克隆/死代码/循环依赖/PII/日志伪造/弱口令；AST 佐证提升置信（W23 卡 B）；AST-only 新规则 PY-NONE-DEREF；测试盲区 TOP20（untested_hotspots，methodology 诚实标注）。严重度四级 + 置信度 + 误报治理（金标 P=1.0 离线零误报）。**独立复验曾定性：13 个 critical/high 命中全为形态误报的问题已由后续波次治理**（audit-self 自审计 exit 0）。
- **缺陷**：污点传播仍是单步/两步启发（SQL 常量单步传播 W20-D），无全路径污点追踪（对标 CodeRabbit 的数据流分析属落后）；OWASP 覆盖以注入/密钥/反序列化为主，SSRF/XXE/权限类缺规则。
- **本轮证据**：金标离线复现 P=1.0/R=0.8444；Java 三规则 8 命中零误报。

#### 1.3 重构能力 —— 评分 5.5/10（落后，代际缺口）
- **状态**：⚠️ 部分具备
- **对标**：JetBrains（确定性重构原语）/ Aider（多文件编辑+git diff 验证）
- **现状**：proposal→patch→apply-to-source 闭环（W23 卡 A：target_sha256 指纹防覆盖、all-or-nothing、默认 dry-run、--yes 二次确认）；apply --commit-message/--pr-description（P0-8）；签名兼容比对（compat_notes）；LLM diff 概率性修复 verified 闸门。
- **缺陷**：仍无 extract-function/safe-rename/inline/move 等确定性原语；跨文件接口变更级联不做；重构后自动更新调用方无（文档/测试联动靠 LLM 概率性）。与 JetBrains 安全边界对标仍属**落后**——这是连续三轮审计的定性，未恶化也未改善。
- **改进方案**（不变，重申最短路径）：以现有 references() 调用图 + tree-sitter 为底座先做 safe-rename（python 单语言 MVP）：rename 符号 → callers 反查 → 逐文件 token 级替换 → AST 复检 → demo dogfood 20 符号 ≥18 verified 为验收门。

#### 1.4 变更与验证 —— 评分 8/10（持平偏上）
- **状态**：✅ 已具备
- **对标**：Aider（自动测试回归）/ CodeRabbit（变更摘要）
- **现状**：受影响测试聚焦（run_existing_tests callers 反查 + exit 5 整库兜底）；语法校验+沙箱测试闭环 verified/needs-review 双态；commit message/PR 描述生成（P0-8 已清偿）；重构失败不落盘（apply all-or-nothing + 指纹不符拒绝）。
- **缺陷**：Lint/类型检查未自动触发（无 mypy/ruff 接入验证层）；失败回滚是"不写入"而非"写后回滚"（apply 失败中途断电无 undo——概率极低但存在）。

### 二、用户体验（权重 20%）—— 66/100

#### 2.1 交互设计 —— 评分 6.5/10（持平）
- **现状**：CLI 进度实时走 stderr（W24-B，--quiet 可关、--check stdout 纯净）；报告 md/html/json 三格式按严重度/文件分组；`codeaudit diff` 三栏对比（本轮深审 `comparator.py`：±3 行最近匹配、稳定排序、title 回退身份防跨源误配——口径正确）；doctor/init 已具备。
- **缺陷**：diff 是文本三栏非 side-by-side 逐 hunk 接受/拒绝（对标 GitHub PR 视图落后）；长任务无百分比/ETA（进度是阶段级非条目级）；Web 端 resume 入口未接线（server 侧 resume 仍归集成人，docs/23 明示）。

#### 2.2 信任与可控 —— 评分 7/10（持平）
- **现状**：apply 默认 dry-run + --yes 显式确认 + 指纹防覆盖（危险操作强制确认达标）；只读审计 vs 审计+重构以 --fix 开关区分；事件流全程可追溯（append_event 原子 seq）；报告与事件落库可回放。
- **缺陷**：「哪些自动执行/哪些需确认」无集中文档说明（散在 --help 与 docs）。

#### 2.3 学习与适配 —— 评分 6/10（持平）
- **现状**：.codeaudit.toml 项目级配置（disabled_rules/severity_overrides/ignore_paths，W22-C）+ 自动发现（CWD 回退项目根）；init 模板骨架；doctor 引导。
- **缺陷**：接受/拒绝历史学习无（用户偏好反馈环缺失，对标 Cursor/Cline 的 memory/规则沉淀属缺失）；首次引导仅 init 骨架无交互式向导。

### 三、流程优化（权重 20%）—— 68/100

#### 3.1 审计流程 —— 评分 7/10（+1）
- **现状**：全量→聚焦分层具备（llm_review_top_files 风险聚焦，默认 0=全量待翻）；多 worker 并行（压测 100 并发零 5xx）；增量审计 --diff/--baseline（CI 门禁/PR 自审计 yml 实战）；SARIF 输出接 CI。
- **缺陷**：llm_focus 翻默认仍被 P0-1 对数阻塞（本轮 429 实探再次证实）。

#### 3.2 重构流程 —— 评分 6.5/10（持平）
- **现状**：分析→计划→预览(dry-run)→执行→验证(verified 闸门)→提交(commit message)流水线成型；重构批次以 patch 序号分立可独立应用；PR 描述自动生成。
- **缺陷**：执行顺序无依赖拓扑排序（多 patch 应用顺序靠序号非依赖图）；重构计划无风险评估字段（estimated_effort_hours 有，风险级无）。

#### 3.3 效率优化 —— 评分 7/10（+1）
- **现状**：规则通道 0.239~0.278 s/KLOC（三级口径实测）；工具循环消息预算（agent_message_budget 折叠）；resume 断点续跑（**本轮 W24-E 后 ingest/index/detect 三阶段可跳，detect 是最贵阶段——离线重扫成本直接归零**，流程韧性实质提升）；快速模式（--quick soak 口径）具备。
- **缺陷**：LLM 通道成本治理判据（P50≤300 s/KLOC）未回填（P0-1 阻塞）；已分析未变更文件的结果复用无（每次全量重扫——resume 缓解进程级中断但不覆盖同机重复审计）。

### 四、系统架构（权重 25%）—— 79/100

#### 4.1 整体架构 —— 评分 8/10（持平）
- **现状**：indexer/detect/refactor/llm/orchestrator/sandbox/taskstore/report 分层解耦；契约文件（models.py/indexer/base.py/llm/base.py）接口清晰；自定义规则 register_rule API 可用；单体+多 worker（非分布式，10 万+文件场景实测够用）。
- **缺陷**：detect↔agents 循环引用未收敛（架构债清单在案）；扫描器注册表化未做（规则注册分散）。

#### 4.2 LLM 编排 —— 评分 6.5/10（持平）
- **现状**：Prompt 版本管理（PROMPT_VERSION=v3 + 基准报告头部记录）；输出结构化约束（JSON 解析 + 失败诚实降级——本轮金标复跑中 LLM 增强失败降级启发式的路径再次实战验证）；消融框架 8 组；FakeLLM 离线模式（本轮确认 api_key='' 正确切换 FakeLLMClient，`pipeline.py:122-130`）。
- **缺陷**：**P0-7 未清偿——无 fallback/备用模型路由，429 单点**：本轮单点探针实测 HTTP 429 即全链路降级，w22_focus_compare 对数无法执行。这是连续三轮审计的钉子项。
- **改进方案**（可直接执行）：GlmClient 外包一层 RouterClient（`audit/llm/router.py`）：主模型 429/超时 → 备用模型（GLM_MODEL_FALLBACK env，缺省 glm-4.7-flash 等同账户可用档）→ 全败才降级规则通道并在事件中标注降级链。MVP 验收：单测模拟主 429 备成功 → 报告 llm_calls 分模型计数；429 注入下 demo --fix 仍 verified。

#### 4.3 数据与状态 —— 评分 8/10（+1，本轮重点深审面）
- **现状（W24-E 深审结论：设计站住）**：
  - **状态机**：queued/running/done/failed/interrupted 五态 + mark_resuming 守卫（仅 interrupted/failed 放行，WHERE 原子条件 `taskstore.py:517-533`）；sweep 分流（running+stage_done→interrupted 保副本、running 无进度→failed 回收，`taskstore.py:625-680`）；**带进度 failed 可续跑**语义闭环。
  - **detect 断点产物**：`<task_root>/state/detect.json`（issues+stats 快照），**round-trip 实测无损**（含 evidence 字典列表/patch_id/degraded_ingest 全保留）；两道降级防线（判定层 schema 校验剔除 `pipeline.py:519-520` + 执行层加载失败回落全量重跑 `pipeline.py:743-747`）；落盘尽力而为不阻断（`_write_detect_artifact` 全异常吞 debug）。
  - **resume 重放语义正确**：基线抑制 4b 在 detect resume 块之后独立执行（`pipeline.py:754-756`），resume 后 stats.suppressed 由新扫描重放覆盖，无重复计数。
  - **CLI 状态机**：三态处理完整（不存在/不可续跑/正常），except BaseException → 回落 interrupted 可再续（Ctrl+C 级）；config_json 重建剔除 <redacted> api_key 由 from_env 补全（`cli.py:505-524`），测试断言在场（test_cli_resume.py:146）。
  - **12 新增测试项与申报一致**：cli 7（含 4 种不可续跑状态参数化）+ 集成 2（detect 跳过/损坏回落）+ taskstore 3。
- **缺陷（本轮新发现 2 项）**：
  1. **[P2] CLI resume 无 sweep 前置，SIGKILL 级硬杀留死任务**：触发条件=resume 运行中进程被 SIGKILL（非 Ctrl+C）→ 任务滞留 running → 再次 `codeaudit resume` 报「状态为 running，不可续跑」退出 1，且 CLI 无清扫入口（sweep_interrupted 仅 server/app.py:285 启动调用，TaskStore 构造不 sweep）。预期：CLI 侧能自愈。实际：需先起一次 server 才能解锁。→ 建议：cmd_resume 对「running 且 stage_done 非空」的任务先调 sweep_interrupted(grace_seconds>0) 或直接放行复用 mark_resuming 语义（mark_resuming 放行集扩 running+进度亦可，SQL 条件同步改）。
  2. **[P3] 离线 FakeLLM 模式下 understand 增强警告观感**：api_key='' 纯规则模式必然打「架构理解：LLM 增强失败，降级为启发式结果：no parsable JSON in response: ''」——行为正确（FakeLLM 空文本→诚实降级）但措辞误导（用户会以为环境出问题）。→ 建议：FakeLLMClient 时静默或改「离线模式：跳过 LLM 增强」。
- **其他**：结果版本化仍未做（report_json 单列 UPDATE 覆盖，历史对比靠 diff 子命令对两份落盘报告——P2 遗留）；敏感信息治理达标（api_key 落库脱敏 + 规则 snippet 打码 + 本轮确认脱敏值不回传链路有测试钉住）。

#### 4.4 可靠性与工程质量 —— 评分 8.5/10（持平）
- **现状**：全量 2152 绿（350s 实测）+ coverage 棘轮 85 + pip-audit + ruff 全域绿 + 72h soak 自动化巡检 27/36 全 PASS；错误重试（LLM 3 次指数退避/SQLite busy_timeout+写重试）；配置三级优先级（env > .env > 默认 + CLI 覆盖）；事件链路可追踪。
- **缺陷（本轮新发现 1 项）**：
  3. **[P2] bench.run CLI 官方金标复现口径坑（可复现性缺陷）**：本轮实测，`python -m bench.run --projects ...` 不带 `--goldset` 时默认加载 demo_proj GOLDEN_ISSUES.md 仅 **12 条金标**，且主路径不传 `config_overrides={'api_key':''}`——.env 有 key 时混入 LLM 通道（429 下降级），得到 **P=0.123/R=0.100 的"伪回归"结果**（本轮 audit_round5 首跑即踩中，后以 `--goldset bench/datasets/goldset.jsonl + api_key=''` 复现出正确 1.000/0.8444）。240 金标正确姿势无任何文档/脚本固化。触发条件=任何人按 README 直跑 CLI 复现指标。预期：默认口径即官方口径。实际：默认口径是"12 条金标 × 10 项目"的错配 + LLM 噪声。→ 建议：①run.py CLI 默认 --goldset 改 bench/datasets/goldset.jsonl；②加 --offline 开关（透传 api_key=''）；③把第五轮复现命令写进 README/bench 脚本固化。
- **监控**：verify_stats/ast_wiring 进报告「检测质量观测」节（W24-B 已清偿，上轮遗留清零）。

---

## 第三部分：P0 缺失项清单（第五轮更新版）

上轮 P0 八项清偿状态：**P0-2/3/5/8（W23 四卡）+ P0-4/6（W24 三卡）已清偿并经本轮实证复核**；P0-1/P0-7 仍阻塞。新增 P0-9/P0-10。

| # | 缺失项 | 为什么是 P0 | 依赖/顺序 | MVP 验收标准 |
|---|---|---|---|---|
| P0-1 | 在线成本对数与 llm_focus 翻默认 | 1398 s/KLOC 历史实测未证伪；翻默认无判据即外引属裸奔 | 仅依赖 GLM 配额窗口可用（无充值依赖，429=套餐限流）；窗口开时第一时间跑 `python -m bench.w22_focus_compare` | 对数报告落盘：full vs llm_focus 两组 P50/P90 s/KLOC + Precision；判据 P50≤300 且 P 降幅≤3pp → 翻默认 + README 更新 |
| P0-7 | 模型路由 + fallback | 本轮单点探针再次实证：主模型 429 即全链路降级，单点无兜底 | 无外部依赖（备用模型同账户档位即可）；可与 P0-1 并行开发、对数联动 | RouterClient + GLM_MODEL_FALLBACK；单测注入主 429→备成功；429 注入下 demo --fix 仍 verified 且事件标注降级链 |
| P0-9（新） | resume 全接线：server 侧入口 + CLI sweep 自愈 | CLI 能续跑但 server 不能（docs/23 明示归集成人）；SIGKILL 硬杀留 running 死任务无 CLI 解法（F5-R2） | 无外部依赖 | server POST /api/audits/{id}/resume + cmd_resume 对 running+进度任务 sweep 后放行；集成测试各 2 条 |
| P0-10（新） | bench 官方口径固化 | 官方指标复现命令无文档且默认口径错配（F5-R1），伪回归会污染一切对外引用 | 无外部依赖，半小时级 | --goldset 默认值改 goldset.jsonl + --offline 开关 + README「指标复现」节；复现命令一条直出 1.000/0.8444 |
| 存量 | 确定性重构原语 / 污点传播 / Go 语言包 / 结果版本化 | 代际差距项（对标 JetBrains/CodeRabbit 的核心面），连续三轮在案 | safe-rename 依赖 references()（已具备）；Go 依赖 Java 模式复制（已具备跟随条件） | 沿第四轮报告验收门不变 |

## 第四部分：重构路线图

- **短期（1-2 周）**：①用户验收提交 W24-E 工作区改动 + push + 打版 0.7.0（bb471c0 + W24-E，10 文件）；②P0-10 口径固化（半小时级，最便宜）；③P0-7 RouterClient（1 天级，纯离线可开发+单测）；④P0-9 两件（server resume 入口 + CLI sweep 自愈）；⑤P0-1 对数（等配额窗口，窗口开即跑，判据回填后决定翻默认）。**验收指标**：全量 pytest ≥2152 绿、金标双门零回退、doctor 0 失败。
- **中期（1-2 月）**：①Go 语言包（复制 Java 卡模式：tree-sitter-go + 三规则 + 语料 resolved_ratio≥0.4 + E2E 双门）；②safe-rename 确定性原语 MVP（python 单语言，dogfood 20 符号 ≥18 verified）；③结果版本化（report_json 历史表迁移，diff 子命令消费）；④detect↔agents 循环引用收敛 + 扫描器注册表化。**验收指标**：SUPPORTED_LANGUAGES=5、rename verified 率≥90%、report 历史可回放对比。
- **长期（3-6 月）**：①污点传播两级（函数内 def-use 全量 + 过程间一跳摘要，goldset +20 条检出 ≥15）；②extract-function 原语 + 跨文件级联签名变更；③Web 端 PR 式逐 hunk 接受/拒绝（对标 GitHub PR view）；④用户偏好反馈环（接受/拒绝历史 → 规则白名单自动沉淀）。**验收指标**：taint goldset 检出率≥75%、跨文件级联零断链、hunk 级操作可用。

## 第五部分：风险提示

1. **技术债**：resume 跳过集扩展（ingest/index/detect）与 `_RESUMABLE_STAGES` 的"盘上产物"前提强耦合——后续任何阶段想入集都必须先落盘，模块 docstring 已声明该约束，但无代码层断言防呆（建议加注释级契约测试）；arch_layers 白名单硬编码（配置化在案）。
2. **安全**：Docker 后端 opt-in 默认 subprocess（无 CPU/内存/网络限制）——W14 治理后仍属实态；硬链接模式 + --fix 就地改写源项目的风险仍以指纹校验+dry-run 默认缓解，无 undo。api_key 脱敏链路（落库 <redacted>、resume 重建剔除、测试钉住）本轮确认闭环 ✅。
3. **性能瓶颈预判**：LLM 通道 completion:prompt=3:1 异常（历史实测）指向工具循环历史膨胀，agent_message_budget 只护 agent 循环不护 review 循环——P0-1 对数时顺带核对 review 侧 token 分布；10 万+文件场景单机内存（索引全载）未测 >50 万行档。
4. **合规**：规则库 86 条含 GitHub Advisory 种子 27 条（W16-A 核实来源）——散布需保留来源注记；语料/金标文件含合成密钥样例（打码呈现）无泄露实害；无 License 文件风险在案（开源协议待补，若公开仓库需在 0.7.0 前定）。

---

## 附：本轮审计方法与证据清单

- 代码深审：`audit/orchestrator/pipeline.py`（W24-E diff 全量）、`audit/taskstore.py`（mark_resuming/sweep）、`cli.py`（cmd_resume/_rebuild_config_from_task_json）、`audit/report/comparator.py`、`audit/indexer/java_extract.py`、`bench/run.py`、`bench/w22_focus_compare.py`、`audit/config.py from_env`、`audit/llm/glm_client.py`
- 实测命令：`python -m pytest tests -q`（2152 passed 350.20s）；`ruff check audit cli.py server`；`python demo/run_demo.py`（verified 12.2s）；金标复现（run_bench + goldset.jsonl + api_key=''，P=1.0/R=0.8444/F1=0.9157）；Java E2E（run_audit on tests/corpus/java，8 命中零误报）；AST 13/13；`python cli.py doctor`；GLM 单点探针（HTTP 429）；Issue/AuditStats round-trip 脚本（无损）
- 产物：`bench/results/audit_round5_goldset_20260918.md`（正确口径金标复现）；首跑异常结果已被本文件覆盖修正（12 条金标伪回归样本，教训记入 F5-R1）
- 按约束：未 commit、未 push、未改任何源码（bench/results 新增 1 个报告文件 + 本报告）
