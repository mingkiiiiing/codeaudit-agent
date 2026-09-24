# 第六轮全维系统性审计报告（2026-09-19）

> 审计对象：代码库级智能审计与重构 Agent（CodeAudit Agent）
> 审计轮次：第六轮（工作流路由：上轮=工作 W25 → 本轮=审计）
> 审计基线：bb471c0（W14~W24 已提交）+ 工作区 W24-E 与 W25 改动（未提交）
> 上轮对照：audit_round5_20260918_复审.md（71/100 B）

---

## 第一部分：总览

- **目标 Agent**：CodeAudit Agent（GitHub mingkiiiiing/codeaudit-agent）
- **版本状态**：v0.6.0 已打版；bb471c0 已提交未 push；**W24-E + W25（P0-10/7/9 三卡）全部在工作区待提交**
- **审查日期**：2026-09-19
- **综合评分：72 / 100（B）**，较第五轮 71 分 **+1**
  - 功能实现 71（35%）｜ 用户体验 67（20%）｜ 流程优化 69（20%）｜ 系统架构 81（25%）
- **总体评级：B**
- **一句话结论**：W25 三卡全部实证站住——RouterClient 设计干净（统计链经 usage_totals 无变形、条件接线零行为变化）、resume 自愈与 server 端点语义闭环（mark_resuming 的 WHERE 原子条件天然防并发双续跑）、bench 官方口径一条命令可复现；本轮走读新发现并实证一个 P2 安全一致性缺口（server resume 绕过 SOURCE_ROOTS 白名单，已复现 200 vs 创建路径 400），半小时级可修；P0-1 对数第三次被 429 实探钉死（09-19 仍限流）。

### 本轮实测数据（全部现场复跑）

| 验证项 | 结果 | 判定 |
|---|---|---|
| 全量 pytest | **2176 passed, 2 skipped**（306.55s） | ✅ 与 W25 收口基线逐位一致 |
| ruff（audit/cli/server/bench 全域） | All checks passed | ✅ |
| demo 端到端闭环 | verified，9.6s | ✅ |
| 金标一条命令复跑（--offline，240 标签/10 项目） | **P=1.000 / R=0.844 / F1=0.916**（round6_goldset_20260919.md） | ✅ 零回退门连续三轮成立 |
| GLM 单点探针 | **HTTP 429**（09-19 仍限流，连续第三次实证） | ❌ P0-1 对数继续阻塞 |
| resume 白名单绕过复现 | CODEAUDIT_SOURCE_ROOTS 指向无关根时，创建 400 / **resume 200** | ⚠️ 新发现 F6-R1（P2） |
| 72h soak 巡检 | 28/36 全 PASS（预计 09-19 内收口），空转自动化保持停用 | ✅ |

---

## 第二部分：分维度审查报告（本轮重点面 + 评分变动依据）

### 一、功能实现 71/100（+1）

**W25-B RouterClient 深审（P0-7 架构面清偿确认）**
- **状态**：✅ 已具备（MVP 形态）｜ **对标**：OpenHands（多模型路由）/ Aider（模型降级切换）｜ **水平**：持平（一主一备 MVP；跨服务商路由/多级 fallback/熔断记忆未做，docstring 明示边界）
- **现状**：`audit/llm/router.py` —— 仅捕获 LLMError（主模型重试耗尽终态）触发备模型一次机会，ConfigError 原样上抛（配置错误换模型无益，语义正确）；主备独立 GlmClient 实例（缓存键含 model 双保险、失败响应不入缓存无污染）；双败上抛备模型异常并 `from` 链主异常（上下文完整）；`fallback_used` 线程安全计数（含备也失败的切换，语义诚实）。
- **统计链验证**：pipeline 经 `usage_totals()` 委托消费（pipeline.py:221-223），RouterClient 对主备逐键求和（router.py:119-126）——备模型完成的调用其 token 必然可见，统计口径无变形 ✅。接线为条件构造（fallback_model 非空且≠主模型才包 RouterClient，pipeline.py:135-139），缺省行为与现状逐字节一致 ✅。
- **边界（如实定性，非缺陷）**：备模型与主模型共用同一 api_key/base_url——**账户级 429（当前阻塞 P0-1 的形态）下主备同败**，fallback 解决的是"单模型故障/弃用"而非"账户限流"；跨服务商路由不在 MVP 范围（docstring 明示）。fallback 的真实环境验证在当前 429 下不可执行，本轮以注入测试为准（10 用例全绿）。
- **缺陷清单**：无新缺陷。已知边界两项已由卡 B 如实申报（fallback 触发的 warning 事件未接编排层，观测入口=fallback_used 属性；GLM_MODEL_FALLBACK 只接 from_env，from_sources 路径未扩）。

**其余功能面**：沿第五轮结论无变化——多语言 4/7（Go 待做）、规则通道金标满分、污点启发式、重构原语缺口（safe-rename 连续四轮在案）。金标本轮复跑 P=1.000/R=0.844 零回退 ✅。

### 二、用户体验 67/100（+1）

- resume 自愈（SIGKILL 滞留不再需要起一次 server 解锁）+ server resume 端点补齐 API 入口——任务恢复的用户路径从"仅 CLI、且会被 running 卡死"升级为"CLI/server 双入口 + 自愈"，UX 韧性 +1。
- 沿第五轮：CLI stderr 进度/doctor/init/diff 三栏对比达标；PR 式逐 hunk 审阅、用户偏好学习环仍缺（对标 GitHub PR / Cursor 落后，连续三轮在案）。

### 三、流程优化 69/100（+1）

- **P0-10 清偿确认**：`bench/run.py` —— `--goldset` 默认 goldset.jsonl（仓库根锚定 DEFAULT_GOLDSET_JSONL 常量，任意 CWD 正确）、`--offline` 透传 config_overrides 且消融路径 base_overrides 同步（卡 A 超出任务卡的最小合理增量，同根修复）；README「指标复现」节一条命令直出官方口径。**本轮实测：一条命令复现 P=1.000/R=0.844/F1=0.916、240 条**——第五轮的"伪回归"坑已不可能再踩。`build_goldset(None)` 的库级默认路径保留（向后兼容，既有用例零改动）。
- resume 全接线使断点续跑覆盖 CLI/server 双通道（流程韧性）；P0-1 对数仍阻塞（09-19 探针 429，第三次实证——llm_focus 翻默认判据继续缺席）。
- 其余沿第五轮：多 worker 并行/增量审计/CI 门禁达标；多 patch 无依赖拓扑排序在案。

### 四、系统架构 81/100（+2，本轮变动最大维度）

**W25-C resume 全接线深审（P0-9 清偿确认 + 新发现一处缺口）**
- **状态机**：`_resume_guard_action` 纯函数（无 I/O，CLI/server 单源零复制）——interrupted→resume / failed+进度→resume / running+进度→sweep / 其余→reject，语义与 W24-E「不可续跑」逐字节兼容 ✅。
- **并发安全验证**：并发双 resume 场景由 mark_resuming 的 `UPDATE ... WHERE status IN (interrupted, failed)` 原子条件天然防护——第二个调用 rowcount=0 → 409「状态复位失败」✅（走读确认，未单测并发——建议后续补并发用例，见缺陷 3）。
- **sweep 自愈语义**：running 滞留先 sweep（grace=CODEAUDIT_SWEEP_GRACE_SEC 缺省 30s）再重读，仍 sweep（窗口内有 updated_at 活动）→ 409「仍在运行或刚有活动」——不冒险双执行体续跑，语义正确；全局清扫的取舍已如实申报（grace 窗口内静默超窗的真活跃任务理论可误判，窗口机制的既定代价）。
- **server 端点**：复用 _launch_audit 后台执行与事件流；**卡 C 自查修正的 audit_id 透传（决定 task_root，不传则断点产物失效）是本轮集成质量的亮点**——该错误未漏到收口。
- **缺陷清单（本轮新发现）**：
  1. **[P2] F6-R1 server resume 绕过 SOURCE_ROOTS 白名单** → 触发条件：`CODEAUDIT_SOURCE_ROOTS` 非空，存在历史任务其 source_path 在收紧后的白名单之外（或 config_json 含越界路径），POST /api/audits/{id}/resume。预期行为：与创建端点同口径 400 拒绝（W15-A2 语义）。实际行为：**实测返回 200 接受续跑并拉起后台审计**（复现脚本：设置 SOURCE_ROOTS=C:\definitely_not_a_real_root，seed 越界任务，resume 200；同路径走创建端点会 400）。缓解面：需持有 API token（鉴权中间件覆盖 /api/*）、或回环无 token 的开发形态；越界路径在创建时本已被校验，逃逸窗口=创建后白名单收紧。→ **修复（半小时级）**：resume_audit 在 config 重建后、mark_resuming 前补一行 `_ensure_source_allowed(Path(config.source_path))`（server/app.py:718 段内）+ 2 条用例（越界 400 / 白名单内正常续跑）。CLI 路径无此问题（SOURCE_ROOTS 本就是 server 治理面，CLI 本地用户为完全信任边界）。
  2. **[P3] resume 端点未接 RATE_LIMIT 准入** → 与创建端点限流口径不一致（卡 C 已如实申报，闸门信号量兜底排队不致过载）；建议随 F6-R1 一并补齐（复用创建端点限流依赖）。
  3. **[P3] 并发双 resume 仅靠 mark_resuming 原子性兜底，无显式并发用例** → 行为正确但无回归钉；建议补一条 ThreadPool 双 POST 断言恰一 200 一 409。
- **其余**：taskstore 状态机零改动（卡 C 只调用，契约纪律 ✅）；bench 口径固化消除了"复现指标被 .env 污染"的工程债；README/CHANGELOG/docs 同步无残留（本轮走读核对）。

### 4.4 可靠性与工程质量（维持 8.5/10）

- 全量 2176 绿（306.55s）连续两次实测、ruff 全域绿、demo verified（9.6s）、金标零回退连续三轮、soak 28/36 全 PASS（预计今日收口，36 次全程零 FAIL）。
- 测试对账：W25 净增 24 用例（bench 4 + llm 10 + CLI 3 + server 7）与收口申报逐位一致；test_server_resume.py 的假流水线/条件等待/上下文管理器形态与既有 server 用例同风格。

---

## 第三部分：P0 缺失项清单（第六轮版）

| # | 缺失项 | 为什么是 P0 | 依赖/顺序 | MVP 验收标准 |
|---|---|---|---|---|
| P0-1 | 在线成本对数与 llm_focus 翻默认 | 1398 s/KLOC 历史实测未证伪；连续三轮 429 实证（09-17/18/19），判据缺席则 W22-B 翻默认无据 | 仅依赖配额窗口；窗口开即跑 `python -m bench.w22_focus_compare` | 对数落盘：full vs llm_focus P50/P90 + Precision；P50≤300 且 P 降幅≤3pp → 翻默认 + README 更新 |
| P0-11（新） | resume 安全一致性（白名单复验 + 限流对齐） | F6-R1 实证：白名单收紧后 resume 可逃逸新策略审计越界路径；server 治理面出现不一致口径 | 无外部依赖，半小时级 | resume_audit 补 `_ensure_source_allowed` + RATE_LIMIT 对齐；越界 400 / 窗口内正常续跑 / 并发双 resume 恰一成功 三条用例绿 |
| 存量 | safe-rename / 污点传播 / Go 语言包 / 结果版本化 | 代际差距项（连续四轮在案），Go 已具备跟随条件（Java 卡模式可复制） | safe-rename 依赖 references()（已具备）；Go 复制 Java 卡验收双门 | 沿第四轮报告验收门不变：rename dogfood 20 符号 ≥18 verified；Go resolved_ratio≥0.4 + E2E 双门；taint goldset +20 检出 ≥15 |
| 催办 | **用户侧：W24-E+W25 提交 + push + 打版 0.7.0** | 工作区积压 19 改动 + 6 新文件跨两轮未入库——任何一次误操作/磁盘故障即全损；评审/简历引用需要可挂 tag 的版本 | 无 | bb471c0 之上一个累积提交（或按 W24-E/W25 拆两个），tag v0.7.0 + Release |

## 第四部分：重构路线图

- **短期（1-2 周）**：①P0-11 白名单+限流对齐（半小时级，第六轮工作轮首选）；②用户提交 W24-E+W25 + 打版 0.7.0；③P0-1 对数（等窗口）；④并发双 resume 用例补钉（P3）。**验收指标**：全量 pytest ≥2176 绿、金标零回退、resume 越界 400 实测。
- **中期（1-2 月）**：①Go 语言包（tree-sitter-go + 三规则 + 语料双门）；②safe-rename MVP（python 单语言）；③结果版本化（report_json 历史表 + diff 消费）；④fallback 事件接编排层 + from_sources 暴露 GLM_MODEL_FALLBACK。**验收指标**：SUPPORTED_LANGUAGES=5、rename verified 率≥90%、报告历史可 diff。
- **长期（3-6 月）**：①污点两级传播（goldset +20 检出 ≥15）；②extract-function + 跨文件级联；③PR 式逐 hunk 审阅；④用户偏好反馈环；⑤跨服务商模型路由（fallback 的 base_url/api_key 可配置化）。**验收指标**：taint 检出率≥75%、级联零断链、hunk 级操作可用。

## 第五部分：风险提示

1. **技术债**：resume 可跳过阶段集与"盘上产物"前提的耦合（第五轮在案，无断言防呆）；`_launch_audit` 复用使创建/resume 共享后台执行机制——resume 失败路径的 failed 兜底与创建路径一致性依赖隐式约定，建议补集成断言。
2. **安全**：F6-R1 白名单缺口（本轮新发现，P2，半小时级修复）；fallback 同账户限流下主备同败（设计边界，非洞）；subprocess 沙箱资源限制缺失（多轮在案）；api_key 脱敏链路连续两轮确认闭环。
3. **性能瓶颈预判**：LLM completion:prompt=3:1 异常的历史实测仍待 P0-1 对数复核（agent_message_budget 只护 agent 循环）；>50 万行单机内存未测。
4. **合规**：License 文件未定（公开仓库 0.7.0 前必须补）；GitHub Advisory 种子 27 条的来源注记需随规则库扩散保留。

---

## 附：审计方法与证据清单

- 代码走读：`audit/llm/router.py`（全文）、`audit/orchestrator/pipeline.py`（_make_llm/usage_totals 消费链）、`server/app.py`（resume 端点全文 + _ensure_source_allowed + 安全配置惰性解析）、`cli.py`（_resume_guard_action/_resume_sweep_grace_seconds）、`bench/run.py`（diff 全量）、`tests/unit/server/test_server_resume.py`（harness 形态）、tests/unit/server/conftest.py
- 实测：`python -m pytest tests -q`（2176 passed, 306.55s）；`ruff check audit cli.py server bench`；`python demo/run_demo.py`（verified 9.6s）；`python -m bench.run --projects <10项目> --offline`（P=1.000/R=0.844/F1=0.916，round6_goldset_20260919.md）；GLM 单点探针（HTTP 429）；resume 白名单绕过复现脚本（TestClient + 假流水线，200 vs 创建 400）
- 按约束：未 commit、未 push、未修改任何源码（bench/results 新增 2 个报告文件 + 本报告）
