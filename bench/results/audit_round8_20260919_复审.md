# 第八轮全维系统性审计报告（2026-09-19）

> 审计对象：代码库级智能审计与重构 Agent（CodeAudit Agent）
> 审计轮次：第八轮（工作流路由：上轮=工作 W27 → 本轮=审计）
> 审计基线：bb471c0（W14~W24 已提交）+ 工作区 W24-E/W25/W26/W27 改动（未提交）
> 上轮对照：audit_round7_20260919_复审.md（73/100 B）

---

## 第一部分：总览

- **目标 Agent**：CodeAudit Agent（GitHub mingkiiiiing/codeaudit-agent）
- **版本状态**：v0.6.0 已打版；bb471c0 已提交未 push；**W24-E~W27 四轮工作区积压（30+ 改动 + 12 新文件）待提交**
- **审查日期**：2026-09-19
- **综合评分：73 / 100（B）**（加权 73.45，较第七轮 72.65 实质 +0.8；分维度功能/流程/架构各 +1，综合因四舍五入与上轮持平）
  - 功能实现 73（35%）｜ 用户体验 67（20%）｜ 流程优化 70（20%）｜ 系统架构 82（25%）
- **总体评级：B**
- **一句话结论**：W27 三卡全部实证站住（F7-R1 根治后同进程重复 from_env 口径一致、报告历史并发 seq 钉住、safe-rename dogfood 20/20 终树复现）——**连续三轮审计各挖出一个新缺陷（F5-R1/F6-R1/F7-R1）后，本轮走读首次零新发现，代码质量呈收敛信号**；soak 出现 29 轮以来首次 FAIL（2 任务超窗未终态、零 5xx 零异常，自动化已自附归因判据）；P0-1 对数第七次被 429 钉死。

### 本轮实测数据（全部现场复跑）

| 验证项 | 结果 | 判定 |
|---|---|---|
| 全量 pytest | **2277 passed, 2 skipped**（326.87s） | ✅ 与 W27 基线逐位一致 |
| ruff 全域（audit/cli/server/bench） | All checks passed | ✅ |
| demo 端到端闭环 | verified，9.0s | ✅ |
| 金标一条命令复跑（--offline，240 标签） | **P=1.000 / R=0.8444**（round8_goldset_20260919.md） | ✅ 零回退连续七轮 |
| safe-rename 终树实测 | calc_total→compute_total：3 替换点跨 2 文件、apply ok、注释/docstring/字符串陷阱按预期保留、幂等成立 | ✅ |
| GLM 单点探针 | **HTTP 429**（连续第七次） | ❌ P0-1 对数继续阻塞 |
| 72h soak 巡检 | **29/36，首次 FAIL**（02:16：终态率 94.6%，2 任务超窗未终态；零 5xx/零异常/零 failed）；空转自动化保持停用 | ⚠️ 自动化已自附归因判据，待 36/36 收口定性 |

---

## 第二部分：分维度审查报告（本轮重点面 + 评分变动依据）

### 一、功能实现 73/100（+1）

**W27-C safe-rename MVP 深审（确定性重构原语首次落地确认）**
- **状态**：✅ 已具备（MVP，纯模块）｜ **对标**：JetBrains（确定性重构原语）/ Rope（python 重命名库）｜ **水平**：落后→部分收敛（原语可用且有 dogfood 门，但无 IDE 级作用域分析、无 CLI/server 入口）
- **越界防护论证核可**（本轮重点）：替换走 tree-sitter identifier 节点字节区间拼接，**字符串（string/string_content）与注释（comment）子树中不存在 identifier 节点——token 级替换物理上不可能越界**，比「正则加边界」的通用做法更强（结构保证而非约定保证）；f-string 插值段 identifier 是真实表达式节点照常替换；import 模块路径组件跳过（`from m import old` 导入项照常改）。多定义点（同名不同作用域）宁拒不改（MVP 口径，报备）。
- **终树实测**：`calc_total→compute_total` 3 替换点跨 2 文件、apply 写入 2 文件、docstring 陷阱「calc_total 汇总金额」与注释/字符串陷阱按预期保留旧名、二次 plan 旧名 0 patches + 明确中文报错（幂等）。
- **dogfood 门**：20/20 verified（W27 收口 + 本轮 pytest 回归 70 绿）。
- **边界（如实）**：动态引用（getattr/反射）不动；`__all__`/重导出联动不做；仅 python；**CLI/server 接线未做**——能力存在但用户尚不可达（`codeaudit rename` 子命令归下轮），故本项不计满。

**W27-B 结果版本化深审（数据与状态维度补课确认）**
- **现状**：`report_history` 表（PK(audit_id,seq)）+ `set_report` BEGIN IMMEDIATE 单事务「当前 UPDATE + 历史 INSERT」双写（seq 标量子查询 COALESCE(MAX)+1，与 append_event 同款原子形态）；`get_report(seq=)` 可选参数缺省路径零变化；delete/prune 级联（同 ID 重建 seq 归 1）；server `GET /api/audits/{id}/reports`（摘要）与 `/{seq}`（版本 JSON）。+16 用例含并发 set_report seq 连续 1..24。
- **对标**：CodeRabbit（逐 PR 历史对比）/ CodeScene（历史趋势）｜ **水平**：持平（数据面就绪；`codeaudit diff` 接 seq 的消费面未做）。

**W27-A F7-R1 根治复审确认**：`_DOTENV_CACHE` 模块级缓存 + 浅拷贝返回（防调用方污染缓存）+ `is not None` 兜底外部手动置位异常态（测试只复位标志时不抛错）——三个细节都对；同进程重复 from_env / from_sources 口径与首次一致（卡 A 实测 48/48/48，本轮 pytest 回归）。**F7-R1 正式关闭**。

**其余沿第七轮**：多语言 5/7（C++ 缺）、规则通道金标满分 P=1.000、污点启发式、GLM 429（第七次）。

### 二、用户体验 67/100（持平）

无新增 UX 面。safe-rename 尚无用户入口（CLI 接线归后续轮——接线时沿 `codeaudit apply` 的 dry-run 默认 + --yes 确认形态）；报告历史端点已就绪但前端展示未接。其余沿第七轮（resume 摘要 LLM 通道标注为 W27 新增的可观测性改进）。

### 三、流程优化 70/100（+1）

- 结果版本化使「同一任务的历史对比」数据面闭环：resume/重跑不再丢旧报告 + diff 消费面就绪前已有 server 端点——流程韧性 + 数据可追溯 +1。
- F7-R1 根治后，resume 在标准 .env 部署下的默认形态恢复 LLM 深审能力（第七轮指出的「最后一公里」已通）。
- 其余沿第七轮（多 worker 并行/增量审计/CI 门禁达标；多 patch 无拓扑排序在案）。

### 四、系统架构 82/100（+1）

- **W27 新增面复审全部站住，本轮零新缺陷**——连续三轮（F5-R1/F6-R1/F7-R1）各一个新缺陷后首次收敛。专项核过的三个风险点均干净：①`_DOTENV_CACHE` 浅拷贝防污染 + 异常态兜底；②report_history 原子 seq + 幽灵静默不写历史（与既有语义对齐）+ 级联清理；③rename 的结构级越界防护（见上）。
- 契约纪律：W27 三卡零契约文件改动；卡 B 两端点进 gray_release 冻结断言。
- 其余沿第七轮（P0-11/P0-12 已清偿；单机多 worker；detect↔agents 循环引用在案）。

### 4.4 可靠性与工程质量（8.5/10，关注点转移至 soak）

- 全量 2277 绿连续两次（W27 收口 + 本轮）、ruff 全域绿、demo verified、金标零回退连续七轮。
- **soak 首次 FAIL（29 轮以来）**：02:16 条目——接纳 37 任务终态率 94.6%（2 任务超观察窗未终态），零 5xx/零客户端异常/零 failed（非错误挂死，是「慢」不是「坏」）；RSS 斜率 DEFER 3.036MB/min（与任务数 r=0.75，每任务常数缓存形态）。**归因纪律值得肯定**：自动化未擅自定性，列出判据「连续夜间 FAIL→深挖；孤立→环境噪声」。本轮审计期我的会话处于 sleep（无并行干扰），属干净首 FAIL。**处置建议**：①继续按巡检既定判据观察至 36/36；②若复现，优先核查 run_soak 的任务接纳窗口与单任务超时参数（300s 窗口 × 夜间 CPU 负载波动的组合）；③两任务最终终态与否可在 audits.db 复查（审计未代查——避免在巡检进行期写库）。

---

## 第三部分：P0 缺失项清单（第八轮版）

| # | 缺失项 | 为什么是 P0 | 依赖/顺序 | MVP 验收标准 |
|---|---|---|---|---|
| P0-1 | 在线成本对数与翻默认 | 1398 s/KLOC 历史实测未证伪；**连续七次 429 实证** | 仅依赖配额窗口；窗口开即跑 `python -m bench.w22_focus_compare` | 对数落盘 full vs llm_focus；P50≤300 且 P 降幅≤3pp → 翻默认 + README 更新 |
| 存量-A | safe-rename CLI/server 接线 | MVP 能力用户不可达；接线后重构能力维度才能实质提分 | rename.py 已就绪（本轮终树实测通过） | `codeaudit rename <dir> <old> <new> [--dry-run]` 子命令 + server 端点；沿 apply 的 dry-run 默认/--yes 形态；dogfood 回归 ≥18/20 |
| 存量-B | diff 接历史 seq / 污点传播 / C++ 语言包 | 代际差距项；版本化数据面已就绪只差消费面 | B 依赖 W27-B 端点（已就绪） | `codeaudit diff <id> <id> [--from-seq]`；taint goldset +20 检出 ≥15 |
| 催办 | **用户侧：W14~W27 提交 + push + 打版 0.7.0 + License** | 积压 30+ 改动 + 12 新文件跨八轮未入库（全损风险持续）；License 连续四轮催办 | 无 | 累积提交（或按 W24-E/W25/W26/W27 拆四个）+ tag v0.7.0 + Release + LICENSE 文件 |
| 观察 | soak 首次 FAIL 定性 | 36/36 收口后需出汇总结论（孤立 vs 复现） | 巡检自动化自带判据 | 收口报告：36 轮 PASS/FAIL 分布 + FAIL 归因 |

## 第四部分：重构路线图

- **短期（1-2 周）**：①safe-rename CLI/server 接线（下轮工作轮首选，能力变现）；②用户提交 W14~W27 + 打版 0.7.0 + LICENSE；③P0-1 对数（等窗口）；④soak 收口定性报告。**验收**：全量 pytest ≥2277 绿、金标零回退、rename 子命令 dogfood 回归 ≥18/20。
- **中期（1-2 月）**：①diff 接历史 seq（消费面变现）；②safe-rename 作用域增强（同名不同作用域区分——需作用域分析，摆脱「多定义点宁拒不改」保守口径）；③C++ 语言包评估；④extract-function 原语（rename 模式复制）。**验收**：SUPPORTED_LANGUAGES=6（评估后）、同名校验误拒率<5%、历史 diff 一条命令可用。
- **长期（3-6 月）**：①污点两级传播（goldset +20 检出 ≥15）；②跨文件签名变更级联；③PR 式逐 hunk 审阅（rename/apply 的 diff 面已具备）；④偏好反馈环。

## 第五部分：风险提示

1. **soak 首次 FAIL**（本轮新增关注项）：首/末 RSS 68.6→161.8MB 单窗内涨幅偏大 + 2 任务超窗——「慢」非「坏」，但若后续复现需核查 run_soak 窗口参数与 .codeaudit 累积（数千任务目录）对服务端文件遍历的拖累；巡检自动化自附 Pearson 归因的做法应保持。
2. **技术债**：rename 的「按名全局改」口径在真实仓库（同名 getter/setter 常见）误拒率会升高——作用域分析是接线前应排期的配套；结果版本化无清理策略（量小暂无碍）。
3. **安全**：连续四轮无新安全发现（F6-R1 已清偿、脱敏链路闭环）——维持「安全治理已到位」定性；subprocess 沙箱资源限制缺失为已知常态。
4. **性能**：LLM completion:prompt=3:1 历史异常仍待 P0-1 对数复核；>50 万行单机内存未测。
5. **合规**：**License 文件连续四轮催办未补**——公开仓库（github.com/mingkiiiiing/codeaudit-agent）无 License 即默认保留所有权利，若目标含开源展示需在 0.7.0 一并补。

---

## 附：审计方法与证据清单

- 代码走读：`audit/config.py`（_DOTENV_CACHE 全段 + _load_dotenv 返回路径）、`audit/taskstore.py`（report_history SQL/set_report 双写/get_report(seq)/list_reports）、`audit/refactor/rename.py`（越界防护论证段 + 替换核心）、`server/app.py`（两新端点）、`cli.py`（resume 摘要标注/_DOCTOR_LANGS）
- 实测：`python -m pytest tests -q`（2277 passed, 326.87s）；`ruff check audit cli.py server bench`；`python demo/run_demo.py`（verified 9.0s）；`python -m bench.run --projects <10项目> --offline`（P=1.000/R=0.8444，round8_goldset_20260919.md）；rename 终树实测（plan/apply/幂等三段）；GLM 单点探针（HTTP 429）
- 按约束：未 commit、未 push、未修改任何源码（bench/results 新增 2 个报告文件 + 本报告）
