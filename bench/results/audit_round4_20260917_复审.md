# 第四轮全维度系统性审查报告（W23 后复审，2026-09-17）

- **审计对象**：工作区版本（v0.6.0 + W14~W23 全部未提交改动，pytest 2036 用例口径）
- **承接关系**：第三轮全维审计（`audit_round3_20260917_复审.md`，66/100，P0 八项）→ W23 工作轮四卡清偿（P0-2/3/5/8）后的首轮复审
- **审查方法**：W23 四卡逐文件实读取证（`applyer.py` 566 行 / `ast_util.py` 203 行 / `testcoverage.py` / `commitmsg.py` 全文 + `engine.py` 接线点）+ P0 未清偿四项现状复核（grep/导入实测）+ 基线复验（全量 pytest 实跑 / `demo/run_demo.py` 实跑 / 金标引用当日 W23 记录）
- **口径声明**：延续第三轮「提示词一」66 项清单口径，未变化维度以第三轮判定为基线、本轮抽查复核；变化维度（W23 触及面）逐项重判

---

## 第一部分：总览

| 项 | 值 |
|---|---|
| 目标 Agent | 代码库级智能审计与重构 Agent（CodeAudit），工作区 v0.6.0 + W14~W23 |
| 审查日期 | 2026-09-17 |
| 综合评分 | **70 / 100** |
| 总体评级 | **B**（较上轮 66 分 **+4**，未达 A 线 75；B 段顶部） |
| 分维评分 | 功能实现 69（35%）· 用户体验 65（20%）· 流程优化 67（20%）· 系统架构 79（25%） |

**一句话结论**：W23 四卡把「重构闭环断头、AST 断供、盲区不可见、提交信息手写」四个 P0 全部在实现层清偿且金标零回退（P=1.000/R=0.844 逐位一致），apply-to-source 的安全工程（双重指纹核对 + 原子写 + 快照回滚 + all-or-nothing）达到可直接对外展示的水准；但第三轮短期路线图里的「快赢三件」（verify_stats 渲染 / CLI 实时进度 / doctor·init）一件未做整批遗留，P0-1/P0-7 仍被 GLM 429 外部阻塞，多语言/增量索引/大仓库三大代际缺口原封未动——70 分的构成里「已做的都做得好，没做的还都是硬骨头」。

### 评分对比（第三轮 → 本轮）

| 维度 | 上轮 | 本轮 | 变化主因 |
|---|---|---|---|
| 功能实现 | 63 | 69 | P0-2 apply 回路（1.3）、P0-3 AST 接线 + PY-NONE-DEREF（1.2）、P0-5 盲区识别（1.2）、P0-8 commit/PR 描述（1.4）四项清偿 |
| 用户体验 | 62 | 65 | apply 默认 dry-run 预览 + 拒绝原因明示（2.1/2.2）；CLI 进度仍缺 |
| 流程优化 | 63 | 67 | 3.2「预览→确认→应用」回路闭合 + PR 描述生成；批次化/拓扑序仍缺 |
| 系统架构 | 78 | 79 | apply 安全工程与 ast_wiring 观测统计；detect↔agents 循环引用等技术债未动 |

---

## 第二部分：分维度审查报告（变化面深审）

### W23-A apply-to-source（1.3/2.2/4.1 交叉面）—— ✅ 已具备（本轮新增），8.5/10

**对标**：JetBrains IDE 重构预览（dry-run diff + 显式应用）/ Aider（auto-confirm 分级 + git 安全网）。**对标水平：安全边界持平偏上（同类开源 Agent 少有做到指纹级防覆盖的）；功能面仍窄（仅 Patch，不含 RefactorProposal）**。

**现状描述**（`audit/fix/applyer.py` 全文 566 行实读）：
- 默认 dry-run 不落盘，`--yes` 才写入（`applyer.py:336/342`），CLI 与服务端同语义；
- **双重指纹核对**：规划期逐 patch 校验 `target_sha256`（`:221-230`），写入期对每个目标**再读一次磁盘**核对（`:461-472`）——封住了规划与写入间隙的 TOCTOU 窗口，这是超出任务卡要求的防御深度；
- all-or-nothing：任一拒绝整体不落盘（`:447-449`）；写入中任一失败 → `_snapshot` 字节留底整体回滚（`:456/494-496`）；
- 写入为同目录临时文件 + `os.replace` 原子替换（`:311-323`）；CRLF 归一化保真、UTF-8→GBK 严格解码拒绝二进制（`:132-139`）；
- 路径安全复检复用 `patcher._path_error`（`:383-391`，单一事实源）；新增文件必须不存在、删除型目标单独核对指纹（`:411-431`）；
- 老报告无指纹：dry-run 可预览、落盘拒绝并提示重新审计（`:394-405`）——诚实降级而非静默放行。

**缺陷清单**：
1. [P2] symlink 目标语义未定义：规划期 `read_bytes` 穿透 symlink 读目标内容，写入期 `os.replace` 把 symlink 本身替换为普通文件——不产生越界写（安全侧无恙），但用户会静默丢失符号链接。触发：目标文件是 symlink → 预期：拒绝或明确提示 → 实际：替换为普通文件。
2. [P2] `_snapshot` 对全部目标文件整字节留底在内存（`:286-294`）——补丁规模受 `fix_max_patches=50` 约束故可接受，但大文件多补丁场景内存峰值无护栏。
3. [P2] 同文件多补丁顺序链依赖「后一补丁指纹 = 前一补丁产物」的隐式约定（`:460-468` 注释自认），生成侧（fix stage）并无断言保证该约定成立——当前单文件多补丁是罕见路径，风险低但约定应显式化。

**改进方案**：symlink 检测一行为 `Path.is_symlink()` → 拒绝并说明（半小时）；snapshot 落盘到 `.codeaudit/<id>/apply_backup/` 替代内存（2 小时）；fix stage 产出多补丁同文件时写断言（1 小时）。

### W23-B AST 接线 + PY-NONE-DEREF（1.2 面）—— ✅ 已具备（本轮新增），7.5/10

**对标**：Semgrep（AST 模式规则）/ SonarQube（符号级判定）。**对标水平：从「落后两个代际」收窄到「落后一个代际」——有了 AST 地基但只做佐证灰度，未成规模迁移**。

**现状描述**：
- `AstParseGate`（`ast_util.py:50-97`）降级路径完备：环境开关 `CODEAUDIT_DISABLE_AST=1` / 解析失败 / 无解析器 / 单文件超 2s 预算熔断同批次——四条路径全部返回 None 绝不阻断，熔断状态不跨进程（Windows spawn 安全，注释明示）；
- 引擎双路径同口径接线：`build_rule_contexts` 与并行 `_scan_file_worker`（`engine.py:167/286`）都传 `ctx.tree`，接线统计落 `extra["ast_wiring"]` 可观测；
- `confirm_hit` 只加不改（行号/severity/文案不动，置信 0.7→0.8，`ast_util.py:137-146`），佐证失败行级命中原样保留——**金标 P=1.000/R=0.844 逐位一致（W23 实测）证明行为等价铁律兑现**；
- 新规则 PY-NONE-DEREF 为 AST-only 保守起步（tree=None 不产命中），17 用例含 3 反例零误报。

**缺陷清单**：
1. [P1] AST 佐证只覆盖 3 条既有规则 + 1 条新规则，82 规则主体仍是行级启发式——第三轮「检测引擎 AST 断供」的 P0 性质已除（地基在、通路在），但「数据流级安全分析」仍无：跨行污点传播、跨过程空值流不在本轮口径。这不是 W23 的欠账（任务卡口径即灰度三条），是下一阶段的代际工程。
2. [P2] `ast_wiring` 统计进了 `ctx.extra` 但报告未渲染——与 verify_stats 同病（记而不显），读者无法知道本次审计 AST 生效率。
3. [P2] 熔断粒度是「本批次」：worker 各持 Gate 实例，一个病态文件只熔断自己 worker 的批次，其他 worker 仍会撞同一病态输入重复付 2s×N。

**改进方案**：`ast_wiring` 随 verify_stats 一并进报告摘要节（随 W24-B 快赢卡）；跨 worker 熔断共享一个 `multiprocessing.Value` 计数或直接在事件流报警（1 小时）；规模迁移以「规则按族批量 AST 化 + 金标逐族回归」节奏推进（长期项）。

### W23-C 测试盲区识别（1.2 面）—— ✅ 已具备（本轮新增），7/10

**对标**：CodeScene（测试热点）/ SonarQube coverage 联动。**对标水平：机制持平、口径诚实度领先（methodology 随节渲染罕见）、精度受索引质量钳制**。

**现状描述**（`testcoverage.py` 实读）：eligible 口径复用 detect 层 is_test_file 并集；触达反查走 `references()` call_edges 含未解析边与全文文本兜底；异常全程兜底 `available=False` 绝不打断报告；TOP 20 + 占比 + methodology 诚实标注（同名符号偏乐观 / 动态调用偏悲观 / resolved_ratio 0.178 放大误差）。

**缺陷清单**：
1. [P1] 名字级反查在 resolved_ratio 0.178 的图上噪音可观——methodology 已声明，但这意味着该功能的实际排期价值随索引质量线性折价；**索引质量（1.1 的 P1 遗留）是本功能与聚焦测试、影响面分析共同的公共放大器**。
2. [P2] 占比分母（eligible 数）与 TOP 20 之外仅 omitted 计数，无「按调用热度排序」——零触达但零调用的死代码会与高频入口混排。

**改进方案**：排序键加调用者数（数据已在 call_edges，半天）；长期随索引 resolved_ratio 提升自动受益。

### W23-D commit message / PR 描述（1.4/3.2 面）—— ✅ 已具备（本轮新增），8/10

**对标**：Copilot Workspace（PR 描述生成）/ Aider（commit message 生成）。**对标水平：模板拼接版持平——确定性可测是优点（15 用例），语义归并/破坏性变更识别是差距**。

**现状描述**：纯字符串拼接零 token；rule_id 经 `Issue.evidence` 的 `rule:XXX` 证据行反查（与 SARIF 同约定，契约零变化）；反查不到诚实降级 `unknown-rule`；PR 描述含逐补丁验证状态表，`tests_run=0` 显式标"未运行"不粉饰；dry-run 输出带「（预览）」标注。

**缺陷清单**：1. [P2] category 取 Issue 既有分类不做语义归并，批量应用时 `fix(general)` 条目可能扎堆；2. [P2] 无 breaking-change 识别（签名变更类 compat_notes 有数据但未进 commit message 头部 `!:` 标记）。

### 其余维度变化点判定（未列出的子项维持第三轮判定）

- **1.1 索引**：无变化（W23 未触碰 indexer/store）。语言 3/7、增量未接线、2000 文件硬上限、resolved_ratio 0.178、继承图缺失——全部维持。**复核方式**：`SUPPORTED_LANGUAGES` 实测仍为 3 语言。
- **1.4 lint/类型检查**：无变化，ruff/mypy/eslint 集成仍缺（第三轮改进方案未被任何一波采纳，连续两轮遗留）。
- **2.1 CLI 实时进度 / 2.3 doctor·init / 偏好学习**：**复核确认零进展**——`grep "cmd_doctor|cmd_init" cli.py` 零命中，事件消费仍不打印。第三轮标注"半天级"的快赢连续一轮未做。
- **3.1/3.3/4.2/4.3**：无变化。P0-1（成本三项翻默认）、P0-7（模型路由 + fallback）维持外部阻塞：GLM 429 余额不足，非代码问题，代码侧 `bench/w22_focus_compare` 与 `model_routing` 方案就绪待对数。
- **4.4 工程质量**：维持 8.5。本轮全量测试与 demo 实测结果见附录（2036 口径 + demo 11.3s verified 实跑通过）。

---

## 第三部分：P0 缺失项清单（第四轮口径，按实施顺序）

| # | 缺失项 | 为什么是 P0 | 依赖/阻塞 | MVP 验收标准 |
|---|---|---|---|---|
| P0-4 | **Java 语言包** | 「多语言」口径 3/7；tree-sitter-java 成熟；W23 的 AstParseGate/confirm_hit 基建可直接复用（依赖已解除，本轮实测 `tree_sitter_java` 未安装、`SUPPORTED_LANGUAGES` 仍 3 语言——**处于"可立即开工"状态却未动**） | 无（基建就绪） | `tree-sitter-java` 接入 parsers + java 符号/imports/调用点提取；语料 10 文件 resolved_ratio ≥ 0.4；JAVA-SQL-INJECTION / JAVA-HARDCODED-SECRET / JAVA-LONG-FUNCTION 三规则移植且金标命中 ≥90%；金标既有 10 项目零回退 |
| P0-6 | **resume 断点续跑 + 报告对比** | 长任务中断=全损（重启 sweep 把 running 置 failed）；两报告 diff 是审计产品基本盘；连续两轮顺延 | 无 | 杀进程重启同任务已完成阶段不重跑（事件流可证）且总耗时 < 全量 50%；`codeaudit diff <id1> <id2>` 输出 fixed/new/persisted 三栏 |
| P0-1 | **成本三项翻默认收尾** | 1398 s/KLOC 在线成本根因"形式已修实质未生效" | **GLM 充值（外部）** | 对数 P50 ≤ 300 s/KLOC 且 Precision 降幅 ≤3pp → 翻默认并回填 docs/21 §5 |
| P0-7 | **模型路由 + fallback** | 429 单点已两次实证（W22-E/W22 对数整轮失败） | **GLM 充值（外部）** | routing 三角色分模型 + 连续 3 次 429 降档记 `extra["llm_fallback"]` + 消融 Precision 降幅 ≤1pp |
| P1-快赢 | **verify_stats 渲染 / CLI 实时进度 / doctor·init** | 三轮遗留的"半天级"项；verify_stats 与 ast_wiring 同为"记而不显"；CLI 零输出是最低级的体验短板 | 无 | 报告摘要节展示 verify 四态与 AST 生效率；`cmd_run` 逐阶段打印 `[3/7] file (n/m)`；doctor 零 Key 出诊断表 + init 写配置骨架 |

**依赖关系**：P0-4 / P0-6 / P1-快赢 三项互相独立可并行（文件所有权可隔离）；P0-1/P0-7 等充值。

---

## 第四部分：重构路线图（第四轮修订）

### 短期（1-2 周，= W24 工作轮）
1. **P0-4 Java 语言包**（首候选）：语言包依赖 + parsers 注册 + java 符号/调用点提取 + content-based 语言识别 + 三规则移植 + 语料金标。
2. **P0-6 resume + 报告对比**：stage_done 粒度持久化 + 重启跳过完成阶段 + `audit/report/comparator.py` 纯模块（CLI 接线随集成）。
3. **P1-快赢三件**：verify_stats/ast_wiring 渲染进报告、CLI 实时进度、`codeaudit doctor/init`。
4. apply 侧 P2 三小件（symlink 拒绝 / snapshot 落盘 / 多补丁断言）。
5. **验收指标**：全量 pytest ≥2100 绿 + 金标 P/R 与基线逐位一致（新增面增量不回退）+ demo 离线闭环 verified + Java 语料 resolved_ratio ≥ 0.4 + 三规则命中 ≥90%。

### 中期（1-2 月）
1. 充值到账 → P0-1 对数翻默认 + P0-7 路由 fallback 落地（消融第 9 组）。
2. 架构债：detect↔agents 循环引用收敛、分层白名单进 `.codeaudit.toml`、扫描器注册表化、Go 语言包视 Java 验收顺延。
3. 规则 AST 化第二族（资源泄漏多形态 / 并发死锁序），每族金标逐位回归。
4. **验收指标**：路由成本降 ≥30% 且 Precision 降幅 ≤1pp；新增强化用例 ≥150；循环引用 grep 零命中。

### 长期（3-6 月）
1. 跨文件级联重构原语（rename/签名变更 → callers 级联 + 逐步验证，对标 JetBrains）。
2. 跨文件污点传播引擎（AST 地基 ×2 族规则后启动，专项基准 P≥0.6 先行）。
3. 索引代际升级：项目级增量缓存 + resolved_ratio ≥ 0.4 + 10 万文件压测基线（解除 2000 硬上限）。
4. 偏好学习（rejected 沉淀 → verify prompt 注入）与 PR bot 形态。

---

## 第五部分：风险提示（增量）

1. **"快赢项被大卡挤掉"成为模式**：第三轮路线图明确的三个半天级项，W23 一张未做——工作轮按 P0 排卡没有错，但半天级项不进卡就永远排不上。W24 起固定「每轮至少一张小卡」惯例。
2. **apply 安全模型对 symlink/特殊文件未闭环**（P2）：当前行为不越界但会静默替换 symlink；Windows junction、硬链接未测。发布 0.7.0 前建议补一轮「目标文件形态」专项用例。
3. **多补丁同文件顺序链是隐式约定**（P2）：生成侧无断言，若未来 fix stage 改为并行产出同文件多补丁，apply 的指纹链会静默失效——建议断言先行。
4. **金标口径资产化**：P=1.000/R=0.844 已连续两轮逐位一致，是行为等价铁律的实证基线；W24 Java 接入将首次**扩**金标语料（新增项目），扩语料后「逐位一致」断言只对既有 10 项目生效——需在 bench 中显式区分「存量零回退」与「增量达标」两个门。
5. 外部阻塞双项（P0-1/P0-7）已跨三轮：若 0.7.0 打版时仍未充值对数，CHANGELOG 必须显式声明「成本收益未验证」而非含糊带过（当前 docs/21 §5 已如实记录，保持）。

---

## 附：本轮审查可复现性（实测记录，2026-09-17）

- W23 四卡核心文件实读：`audit/fix/applyer.py`（566 行全文）、`audit/detect/ast_util.py`（203 行全文）、`audit/report/testcoverage.py`、`audit/fix/commitmsg.py`（结构 + 关键分支）、`audit/detect/engine.py` 接线点（:167/:176-179/:286）。
- P0 现状实测：`python -c "import tree_sitter_java"` → ModuleNotFoundError（P0-4 未动）；`grep -n "cmd_doctor|cmd_init|stage.*print" cli.py` → 零命中（快赢三件未动）；`grep -rn verify_stats audit/report/ cli.py` → 仅注释命中（记而不显维持）。
- 离线闭环：`python demo/run_demo.py` → Patch verified、生成单测通过、总耗时 11.3s、退出码 0（本轮实测）。
- 全量测试：`python -m pytest -q` → **2036 passed, 5 warnings in 333.07s（5 分 33 秒），退出码 0**（本轮实测；5 条 warning 均为金标加载器预期告警，与第三轮口径一致）。
- 金标：引用 W23 当日实测（`w23_goldset_20260917.md`：P=1.000 / R=0.844 / F1=0.916，10 项目 240 标签），本轮未重跑（同日数据 + 全量测试含金标单测回归门）。
