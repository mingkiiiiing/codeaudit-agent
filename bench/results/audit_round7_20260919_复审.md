# 第七轮全维系统性审计报告（2026-09-19）

> 审计对象：代码库级智能审计与重构 Agent（CodeAudit Agent）
> 审计轮次：第七轮（工作流路由：上轮=工作 W26 → 本轮=审计）
> 审计基线：bb471c0（W14~W24 已提交）+ 工作区 W24-E/W25/W26 改动（未提交）
> 上轮对照：audit_round6_20260919_复审.md（72/100 B）

---

## 第一部分：总览

- **目标 Agent**：CodeAudit Agent（GitHub mingkiiiiing/codeaudit-agent）
- **版本状态**：v0.6.0 已打版；bb471c0 已提交未 push；**W24-E/W25/W26 三轮工作区积压（26 改动 + 10 新文件）待提交**
- **审查日期**：2026-09-19
- **综合评分：73 / 100（B）**，较第六轮 72 分 **+1**
  - 功能实现 72（35%）｜ 用户体验 67（20%）｜ 流程优化 69（20%）｜ 系统架构 81（25%）
- **总体评级：B**
- **一句话结论**：W26 三卡全部实证站住（resume 白名单顺序与空路径防御正确、fallback 事件挂点语义干净、Go 语言包 E2E 逐位复现、多语言 5/7）；但本轮配置层走读挖出**全轮次最重要的新缺陷 F7-R1（P1）——同进程第二次 `from_env()` 丢失 .env 值（实测 49→0 字符），使 `codeaudit resume`（不带 --work-root 的最常用形态）与 server 侧 resume 的 LLM 通道静默降级为纯规则模式**；P0-1 对数第五次被 429 钉死（09-19）。

### 本轮实测数据（全部现场复跑）

| 验证项 | 结果 | 判定 |
|---|---|---|
| 全量 pytest | **2239 passed, 2 skipped**（309.25s，本轮无并行干扰） | ✅ 与 W26 基线逐位一致 |
| ruff 全域（audit/cli/server/bench） | All checks passed | ✅ |
| demo 端到端闭环 | verified，8.7s | ✅ |
| 金标一条命令复跑（--offline，240 标签） | **P=1.000 / R=0.8444**（round7_goldset_20260919.md） | ✅ 零回退连续五轮 |
| Go 语料 E2E 终树复核 | **6 命中逐位一致**（SQL3/密钥2/长函数1） | ✅ |
| from_env 二次调用实测 | api_key 长度 **49 → 0**（同进程） | ⚠️ 新发现 F7-R1（P1） |
| GLM 单点探针 | **HTTP 429**（09-19，连续第五次） | ❌ P0-1 对数继续阻塞 |
| 72h soak 巡检 | 28/36 全 PASS（00:14 条目含 RSS DEFER 但已附 Pearson r=0.81 归因分析=每任务常数缓存非时间泄漏）；空转自动化保持停用 | ✅ 今日收口 |

---

## 第二部分：分维度审查报告（本轮重点面 + 评分变动依据）

### 一、功能实现 72/100（+1）

**W26-C Go 语言包深审（多语言 4→5 达标确认）**
- **状态**：✅ 已具备 ｜ **对标**：SWE-agent（Tree-sitter 多语言）/ Semgrep（语言覆盖面）｜ **水平**：持平（5/7；C++ 仍缺）
- **现状**：`go_extract.py` 口径文档完备（package Symbol id 带 `::package::` 段防 func main/package main 主键冲突——开发期自纠；receiver `Type.Method`/`(*Type).Method` 限定；const 全记 constant 按首字母判导出；func_literal 体内/内建函数/泛型实例化不做，边界明示）。跨文件解析 `pkg.F` 按 import 别名→包目录后缀最长匹配→符号确认（store.py:537-569），接收者变量调用尽力解析保留未解析边（有测试固化）。
- **超清单接线核可**：utils.py `.go` 后缀（不接则 guess_language 不认）与 store.py 三处分派（不接则 resolved_ratio=0）均为达成验收门的必要项、非契约文件——集成人 W26 已核，本轮复审维持。
- **实测**：语料 E2E 6 命中零误报、AST 13/13、金标零污染（P=1.000 不变）。
- **已知边界**：doctor 不探测 go（cli.py `_DOCTOR_LANGS` 硬编码，W26 报备）；C++ 缺位。

**W26-A/B 复审确认**：resume 白名单修复（空路径 400 → `_ensure_source_allowed` → mark_resuming，顺序正确）、fallback 事件（finally 收口前、getattr 缺省零事件、CancelledError 上抛语义保留）、from_sources env 层（env > 配置文件 > 默认）——实现与申报一致，测试钉住在场。

**缺陷清单（本轮新发现，本轮最重要产出）**：
1. **[P1] F7-R1 同进程第二次 `from_env()` 丢失 .env 值 → resume LLM 通道静默降级**：
   - **机制**：`_load_dotenv` 以 `_ENV_LOADED` 标志保证"同进程至多实际加载一次"，且**后续调用直接返回空 dict**（config.py:117 `if _ENV_LOADED: return {}`）；GLM_* 三键按设计**不写 os.environ**（防污染），仅经返回值合并进"本次"取值。两者叠加：第二次 `from_env()` 的 env 链为 `os.environ（无）→ dotenv（{}）→ 默认（""）`→ **api_key=""**。
   - **实测复现**：同进程两次 `from_env()`，第一次 api_key 长度 49，第二次 **0**。
   - **踩中路径一（最常用形态）**：`cli.py:583` cmd_resume 不带 `--work-root` 时先 `AuditConfig.from_env().work_root`（消耗进程首次 .env 加载），随后 `_rebuild_config_from_task_json`（cli.py:517）第二次 `from_env(**values)` → **api_key 空** → pipeline `_make_llm` 走 FakeLLM → **resume 静默降级纯规则模式**（仅一条「LLM 未配置」warning 事件可辨，报告/退出码均正常——用户极易漏看）。
   - **踩中路径二**：server 侧 resume——lifespan 启动已消耗首次加载，resume 端点 `_rebuild_config_from_task_json` 的 from_env 必然非首次 → 同样空 key（创建路径不受影响：api_key 来自请求体 config）。
   - **不中招路径**：带 `--work-root` 的 CLI resume（首次 from_env 即在 _rebuild 内）；进程内仅一次 from_env 的路径；W22 起 bench 脚本的 `from_env()+os.environ.setdefault` 自保模式（W22 教训其实就是本坑的第一次露头）。
   - **触发条件**：.env 部署（key 不在真实进程环境）+ 同进程多次 from_env。预期行为：每次 from_env 均能看到 .env 值（加载一次=防重复 IO，不应丢值）。实际行为：第二次起全空。
   - **修复方案（两案，建议 a 先行 b 随后）**：
     - **a（一行级，立即可做）**：cmd_resume 调序——先取任务行、`_rebuild_config_from_task_json`（此时为进程首次 from_env，拿得到 key），再取 work_root 缺省（`args.work_root or config.work_root`），删除 583 行的前置 from_env；+1 用例（无 --work-root 时重建 config 的 api_key 非空——monkeypatch 真实 from_env 链）。
     - **b（结构修，根治所有调用点）**：`_load_dotenv` 解析结果缓存模块级 `_DOTENV_CACHE`，后续调用返回缓存而非 `{}`——"加载一次"语义保留（防重复 IO），"不写 os.environ"防污染设计不变。需回归 F7 既有测试（是否存在依赖"二次为空"的断言需排查）。
   - **预估工作量**：a 半小时；b 半天含回归。

其余功能面沿第六轮：规则通道金标满分、污点启发式、**safe-rename 确定性原语缺失（连续五轮在案，代际缺口之首）**。

### 二、用户体验 67/100（持平）

沿第六轮。F7-R1 的用户感知面：resume 降级有 warning 事件但极隐蔽（报告正常产出），实际是"最常用形态的能力静默缩水"——修复（方案 a）前建议在 resume 中文摘要中显式标注「LLM 通道：未启用（未检测到 API Key）」一行（防御性提示，可作为 P0-12 修复的一部分）。

### 三、流程优化 69/100（持平）

resume 全链路（CLI/server/自愈/白名单）成型使断点恢复成为流程韧性底座；但 **F7-R1 使 resume 在标准 .env 部署下的默认形态失去 LLM 深审能力**——流程能力的"最后一公里"被配置层 bug 卡住，P0-12 修复后该维度才有下一档提升空间。其余沿第六轮（多 worker 并行/增量审计/CI 门禁达标；多 patch 无拓扑排序在案）。

### 四、系统架构 81/100（持平）

- **W26 新增面复审全部站住**：resume 白名单顺序与空路径防御（server/app.py，注释完整）、fallback 事件挂点（pipeline.py:157-180/656，裸客户端引用天然 unwrap，_BudgetGateLLM 零改动）、go 三卡接线（parsers/store/utils 必要且非契约）、卡 C 超清单接线的必要性复核通过。
- **F7-R1 暴露配置层设计债**：`_load_dotenv` 的"加载一次 + 返回空"与"GLM 键不写 os.environ"两个各自合理的设计决策叠加产生系统性丢失——典型的局部正确、组合失效。修复方案 b（缓存 dict）不改防污染语义即可根治，属低成本高收益结构修。
- 其余沿第六轮（P0-11 已清偿；单机多 worker 架构；契约纪律维持——W26 卡 C 两处超清单接线均非契约文件）。

### 4.4 可靠性与工程质量（维持 8.5/10）

全量 2239 绿连续两次（W26 收口 + 本轮，本轮无并行干扰一次通过）、ruff 全域绿、demo verified、金标零回退连续五轮、soak 持续 PASS（巡检自动化对 00:14 条目的 RSS DEFER 主动附了 Pearson 相关性归因——观测质量在进化）。W26 净增 63 用例与申报一致。

---

## 第三部分：P0 缺失项清单（第七轮版）

| # | 缺失项 | 为什么是 P0 | 依赖/顺序 | MVP 验收标准 |
|---|---|---|---|---|
| P0-12（新，P1） | F7-R1 from_env 二次调用丢 .env 值 | **resume 最常用形态（无 --work-root）与 server 侧 resume 的 LLM 通道静默降级**；标准 .env 部署下 W24-E/W25 的 resume 能力打折 | 无外部依赖；方案 a 一行级先行，方案 b 结构修随后 | 方案 a：cmd_resume 调序 + 用例（无 --work-root 时 api_key 非空、resume 报告含 LLM 通道）；方案 b：`_DOTENV_CACHE` + F7 回归全绿；金标/全量零回退 |
| P0-1 | 在线成本对数与翻默认 | 1398 s/KLOC 历史实测未证伪；**连续五次 429 实证**（09-17×2、09-18×2、09-19） | 仅依赖配额窗口；窗口开即跑 `python -m bench.w22_focus_compare` | 对数落盘 full vs llm_focus；P50≤300 且 P 降幅≤3pp → 翻默认 + README 更新 |
| 存量 | safe-rename / 污点传播 / 结果版本化 / C++ 语言包 / doctor 补 go | 代际差距项（safe-rename 连续五轮在案，重构能力评分天花板） | safe-rename 依赖 references()（已具备） | 沿第四轮验收门：rename dogfood 20 符号 ≥18 verified；taint goldset +20 检出 ≥15 |
| 催办 | **用户侧：W14~W26 提交 + push + 打版 0.7.0** | 积压 26 改动 + 10 新文件跨七轮未入库（全损风险）；简历/评审需可挂 tag 版本 | 无 | 累积提交（或按 W24-E/W25/W26 拆三个）+ tag v0.7.0 + Release |

## 第四部分：重构路线图

- **短期（1-2 周）**：①P0-12 方案 a（一行级，下轮工作轮首选）→ 方案 b（结构修+回归）；②用户提交 W14~W26 + 打版 0.7.0；③P0-1 对数（等窗口）；④resume 中文摘要补「LLM 通道：未启用」防御性标注。**验收**：全量 pytest ≥2239 绿、金标零回退、无 --work-root resume 的 api_key 非空实测。
- **中期（1-2 月）**：①safe-rename MVP（python 单语言）；②结果版本化；③doctor 补 go + C++ 语言包评估；④fallback 事件与 P0-1 判据联动（翻默认决策）。**验收**：rename verified 率≥90%、SUPPORTED_LANGUAGES=6（评估后）、报告历史可 diff。
- **长期（3-6 月）**：①污点两级传播（goldset +20 检出 ≥15）；②extract-function + 跨文件级联；③PR 式逐 hunk 审阅；④偏好反馈环。**验收**：taint 检出率≥75%、级联零断链。

## 第五部分：风险提示

1. **技术债（本轮新增定性）**：`_load_dotenv` 的组合失效模式（局部正确、组合有害）提示配置层需要一次"语义级"审查——所有"模块级一次性标志 + 不落全局"的组合都应列出交互矩阵。**已实测坐实双入口同病**：同进程先 `from_env`（api_key 49）后 `from_sources`（api_key **0**）——`from_sources` 的 dotenv 直读同样被 `_ENV_LOADED` 标志短路，两入口共享同一丢失语义。影响面因此扩大：**server 进程内「lifespan from_env → 后续任何 config 构造」的链路普遍丢 .env GLM 键**（创建路径因 api_key 来自请求体而幸免；resume/重建路径全中）。修复 b（缓存 dict）应同时统一两入口。
2. **安全**：F6-R1 已清偿（本轮复审白名单顺序正确）；fallback 同账户限流主备同败（设计边界）；api_key 脱敏链路连续三轮确认闭环。
3. **性能**：LLM completion:prompt=3:1 历史异常仍待 P0-1 对数复核；>50 万行单机内存未测。
4. **合规**：License 文件仍未定（公开仓库 0.7.0 前必须补，连续三轮催办）；GitHub Advisory 种子来源注记需随规则库（现 89 条）扩散保留。

---

## 附：审计方法与证据清单

- 代码走读：`server/app.py`（resume 白名单段全文）、`audit/understand/architecture.py`（F5-R3 分支）、`audit/orchestrator/pipeline.py`（_emit_fallback_event 全文 + finally 挂点）、`audit/config.py`（from_env env_map / from_sources env 层 / **_load_dotenv 117 行 return {}**）、`cli.py`（583 行前置 from_env / 517 行重建）、`audit/indexer/go_extract.py`（头部口径 40 行）、`audit/indexer/store.py`（go 分派 537-569）、`audit/utils.py`（.go 接线）
- 实测：`python -m pytest tests -q`（2239 passed, 309.25s）；`ruff check audit cli.py server bench`；`python demo/run_demo.py`（verified 8.7s）；`python -m bench.run --projects <10项目> --offline`（P=1.000/R=0.8444，round7_goldset_20260919.md）；Go 语料 E2E（6 命中）；**from_env 二次调用实测（49→0，干净进程复核 429）**
- 按约束：未 commit、未 push、未修改任何源码（bench/results 新增 2 个报告文件 + 本报告）
