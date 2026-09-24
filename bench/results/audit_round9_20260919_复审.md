# 第九轮全维系统性审计报告（2026-09-19）

> 审计对象：代码库级智能审计与重构 Agent（CodeAudit Agent）
> 审计轮次：第九轮（工作流路由：上轮=工作 W28 → 本轮=审计）
> 审计基线：bb471c0（W14~W24 已提交）+ 工作区 W24-E/W25/W26/W27/W28 五波改动（未提交）
> 上轮对照：audit_round8_20260919_复审.md（73/100 B）

---

## 第一部分：总览

- **目标 Agent**：CodeAudit Agent（GitHub mingkiiiiing/codeaudit-agent）
- **版本状态**：v0.6.0 已打版；bb471c0 已提交未 push；**W24-E~W28 五轮工作区积压（35+ 改动 + 16 新文件）待提交**
- **审查日期**：2026-09-19
- **综合评分：74 / 100（B）**（加权 74.00，较第八轮 73.45 实质 +0.55）
  - 功能实现 74（35%）｜ 用户体验 68（20%）｜ 流程优化 70（20%）｜ 系统架构 82（25%）
- **总体评级：B**
- **一句话结论**：W28 三卡（rename CLI 接线 / server 端点治理链 / C++ 语言包）全部实证站住——**连续第二轮审计零新实质缺陷**（本轮唯一发现为一处行内注释与实现不一致的文档级瑕疵），safe-rename 自 W27 的「模块级 MVP」升级为「CLI+server 双入口全链路用户可达」，多语言覆盖 5/7→6/7；soak 33/36 归因已闭环（本机负载×固定窗口敏感项，非服务端缺陷）但 36/36 收口未到；P0-1 对数第九次被 429 钉死；工作区积压催办进入第六轮。

### 本轮实测数据（全部现场复跑）

| 验证项 | 结果 | 判定 |
|---|---|---|
| 全量 pytest | **2351 passed, 2 skipped**（346.77s） | ✅ 与 W28 基线逐位一致 |
| ruff 全域（audit/cli/server/bench） | All checks passed | ✅ |
| demo 端到端闭环 | verified，9.7s | ✅ |
| 金标一条命令复跑（--offline，240 标签，10 项目） | **P=1.000 / R=0.844 / F1=0.916，333 报告**（audit_round9_goldset_20260919.md） | ✅ 零回退（第九次确认，连续九轮） |
| rename CLI 终树实测（七段） | 预览 3 替换点跨 2 文件 → 未落盘确认 → --yes 应用 → 终树（import/def/调用点全改，docstring/注释/字符串/旧名相似函数全保留）→ 幂等中文报错 → --diff-only×--yes 互斥拒绝 → --lang go 校验拒绝 | ✅ 全过 |
| diff --from-seq/--to-seq 端到端 | 真实 TaskStore 落库两版 → 对比 fixed=1/new=1 + `#seq=1/#seq=2` 定位标注 + 越界 seq=9 中文报错退出 1 | ✅ |
| C++ 语料 E2E | **6 命中逐位一致**（SECRET3：secret_config.cpp L4/9/10；SQL2：user_dao.cpp L17/22；LONG1：user_service.cpp L15），LOC=420、语言识别 cpp=100% | ✅ 与 W28 申报吻合 |
| GLM 单点探针 | **HTTP 429**（连续第九次） | ❌ P0-1 对数继续阻塞 |
| 72h soak 巡检 | **33/36 已采样**（至 10:16；02:16 起连续 5 轮 FAIL），**归因已闭环**：自动化三轮内完成完整解释——本机另一项目工作负载（fuwai：uvicorn×2+建模批任务）抬升 CPU→单次审计拖慢数倍→固定 300s 窗口末尾 1-2 个在途任务稳定越线；负载期 6 轮 RSS 末值 161.0~164.9MB **零爬升**反向强化无泄漏；零 5xx/零客户端异常/零 failed 贯穿 | ⚠️ 定性=测量敏感项非服务端缺陷；待 36/36 收口出汇总 |

---

## 第二部分：分维度审查报告（本轮重点面 + 评分变动依据）

### 一、功能实现 74/100（+1）

**W28-A rename CLI 接线深审（能力变现确认）**
- **状态**：✅ 已具备 ｜ **对标**：JetBrains IDE（重构安全边界：预览+确认+失败回滚）/ Aider（多文件编辑+diff 验证）｜ **水平**：落后→部分收敛（原语+双入口就绪且有全治理；仍无 IDE 级作用域分析、仅 python 单语言）
- **走读+终树实测**：默认 dry-run（预览 diff + 「未落盘，加 --yes 应用」双重标注）、`--diff-only` 与 `--yes` 互斥**不静默忽略**（显式拒绝防「以为已应用」）、plan errors 中文报错退出 1 绝不落盘、apply 失败 all-or-nothing。七段终树实测全过（见总览表）。`--lang` 传非 python 值经 plan_rename 校验拒绝（`language='go' 暂不支持`），无静默降级。
- **越界防护维持 W27 论证**：tree-sitter identifier 节点区间替换，字符串/注释物理不可越界——本轮终树再次实证（docstring/注释/字符串陷阱保留、`calc_total_str` 相似名不受子串误伤）。

**W28-A diff seq 历史消费（W27-B 数据面闭环）**
- **走读+端到端实测**：seq 只对 audit_id 生效（文件路径给 seq 先行中文拒绝）；`_history_version` 走 CODEAUDIT_DB_PATH 优先口径与 resume/locate_audit 一致；库不存在/版本不存在均 LookupError 中文退出 1；缺省不传 seq 行为零变化（pytest 用例含「缺省定位标注不带 seq=」断言）。W27 遗留的「数据面就绪只差消费面」正式清偿——**同一任务跨 resume/重跑的历史对比一条命令可用**。
- **对标**：CodeRabbit（逐 PR 历史对比）｜ **水平**：持平（CLI 消费面通；前端展示未接，归 UX）。

**W28-B server /api/rename 深审（治理链第一版全前置）**
- **走读确认**：治理顺序=空路径 400 → SOURCE_ROOTS 白名单 `_ensure_source_allowed`（**先于存在性检查**，不泄露越界路径存在性）→ 存在性 400 → plan（`asyncio.to_thread` 防饿死事件循环）→ plan.ok=False 400（计划阶段即拒，apply=true 亦然）→ apply 失败 **409 Conflict**（与 500 的语义区分有选型说明）。鉴权 401/限流 429 由 /api/* 全局中间件覆盖（零端点内代码）；gray_release 冻结断言已同步（tests/integration/test_gray_release.py:55）。9 用例含并发修改 409（注入点=真 plan_rename 薄壳+真实文件写入，apply 保持原实现——注入面最小化）。**与 W15 的 `_ensure_source_allowed` 复用而非复制**（cli.py:656/816 与 server:656/816/1035 同源）。
- **边界（申报内）**：端点未暴露 language 参数（MVP python-only，CLI 的 --lang 已有，server 补齐为小改动，列入中期）。

**W28-C C++ 语言包深审（多语言 5/7→6/7）**
- **状态**：✅ 已具备（首轮口径）｜ **对标**：SonarQube（C++ 规则面）/ CodeScene（多语言符号分析）｜ **水平**：落后（92 条规则对 SonarQube C++ 数百条；但规则质量闸门——熵+字典、AST 佐证保守提升——与自有 python/java/go 家族同构）
- **cpp_extract.py 走读**（232 行）：namespace 嵌套限定拼栈（C++17 `namespace a::b` 与传统嵌套双形态）、类外定义 `ns::Class::method` 限定还原（decl_name 递归剥指针/数组包装）、匿名 namespace 成员沿用外层前缀、宏常量口径（对象宏记 constant/函数式宏明确不记）、#include 剥引号/尖括号、调用点三形态（identifier/qualified/field）。lambda 体整体跳过、模板实例化不做、条件编译不裁剪——边界全部 docstring 申报且语料按此写。
- **rules/cpp.py 走读**（410 行）：CppScan 掩码状态机与 java 版同构（字符串/字符不跨行、未闭合行尾截断防扩散）；SQL 规则「关键字须在字符串内容跨度内 + masked 行上拼接/格式化 hint」双条件（注释不触发）；SECRET 家族复用熵+字典闸门（`#define`/const 声明/普通赋值三形态，R4-7 口令家族放低闸门一致）；AST 佐证保守口径（tree=None 时命中原样保留，绝不删减）。
- **E2E 实测**：6 命中逐位一致 + 反例零误报（pytest 53 用例含）。
- **本轮唯一发现（文档级 Minor）**：`audit/detect/rules/cpp.py:161` 行内注释「内层 break（未闭合字符串）保持 mode 进入下一行」与实现矛盾——139 行 `mode = "code"` 实际将未闭合字符串**重置回 code 模式**（137 行注释「未闭合按行尾截断回 code」才是正确描述）。行为正确（且是防御性的正确方向），纯属从 java 版复制时带来的注释失真。**修复建议**：一行注释订正，随下轮任一卡顺手清偿，不单独立卡。
- **家族级观察项（非本轮新增）**：SECRET 规则在 raw 行上匹配 `_ASSIGN_RE`，块注释内形如 `/* #define API_KEY "sk-..." */` 的行会误报——与 go.py:337/java.py:280 **全家族口径一致**（raw 匹配+值提取需要原文），属既定设计取舍而非 cpp 偏差。记录为家族级改进候选（掩码行预检一列即可，四语言同改），预期误报降幅小（注释内恰好满足熵闸门的完整声明形态罕见），优先级低。

**其余沿第八轮**：规则通道金标满分 P=1.000 维持、污点启发式（两级传播未做）、P0-1 第九次 429。

### 二、用户体验 68/100（+1）

- rename CLI 的信任设计到位：dry-run 默认 + 显式「未落盘」双标注 + 互斥不静默 + 全中文报错——「预览模式」检查项（提示词 2.1/2.2）在 rename 面正式达成；server 端点 apply 缺省 False 同构。
- diff seq 的 `#seq=N` 定位标注让历史对比结果可追溯。
- **未达面**：报告历史的前端展示仍未接（server `GET /reports` 端点 W27-B 就绪，Web 界面无入口）；rename 无前端入口（API-only + CLI）；长任务进度可视化沿旧（CLI --quiet/JSON + server SSE 已有，无百分比/ETA）。
- 其余沿第八轮。

### 三、流程优化 70/100（持平）

- W28 三卡零集成冲突（W26/W27 各 1 处→本轮 0），三卡并行+集成人统一联调流程成熟度持续验证。
- soak 归因闭环值得计入工程流程分但巡检脚本自身改进（drain grace）未落地——持平主因：流程面无新能力，rename 接线是能力变现而非流程变更。
- 其余沿第八轮（多 worker 并行/增量审计/CI 门禁达标；多 patch 无拓扑排序在案）。

### 四、系统架构 82/100（持平）

- **W28 新增面复审零新缺陷**：三卡文件所有权互不相交（cli.py+rename 消费 / server/app.py / cpp_extract+parsers+store+utils+rules），零契约文件改动；store 的 cpp 跨文件解析（`_cpp_ns_files` 最长前缀命名空间匹配 + `_cpp_has_symbol` 后缀匹配）为尽力解析口径且边界申报（接收者变量类型推断不做）。
- **soak 连续 5 轮 FAIL 但归因纪律优秀**：自动化在三轮内完成「窗口收尾即统计→负载源实测（CPU 54%/74% 时段吻合）→跨轮 RSS 末值平台（161.0~164.9MB 零爬升）反证无泄漏」的完整证据链，且未擅自翻「服务端缺陷」定性——这是「监控指标可解释性」的好样本。扣分项：drain grace 改进建议挂在「下波」未动。
- 契约纪律：W28 三卡零契约改动；/api/rename 进 gray_release 冻结断言。
- 其余沿第八轮（单机多 worker；detect↔agents 循环引用在案；结果版本化无清理策略量小暂无碍）。

---

## 第三部分：P0 缺失项清单（第九轮版）

| # | 缺失项 | 为什么是 P0 | 依赖/顺序 | MVP 验收标准 |
|---|---|---|---|---|
| P0-1 | 在线成本对数与翻默认 | 1398 s/KLOC 历史实测未证伪；**连续九次 429 实证** | 仅依赖配额窗口；窗口开即跑 `python -m bench.w22_focus_compare` | 对数落盘 full vs llm_focus；P50≤300 且 P 降幅≤3pp → 翻默认 + README 更新 |
| P0-2 | rename 作用域增强（同名不同作用域） | 「多定义点宁拒不改」在真实仓库（同名 getter/setter、不同类同名方法常见）误拒率会随采用量放大——能力已变现，误拒直接伤信任 | rename.py 既有 AST 基建可扩展 | 同名多定义点按作用域限定名区分：可唯一映射时允许改，歧义时才拒；误拒率较现状 <20%（dogfood 扩样） |
| 存量-A | 污点两级传播 | 代际差距项（对标 CodeRabbit/Semgrep 污点追踪） | 规则引擎 AST 面已就绪 | taint goldset +20 检出 ≥15 |
| 催办 | **用户侧：W24-E~W28 提交 + push + 打版 0.7.0** | 积压 35+ 改动 + 16 新文件跨九轮未入库（全损风险持续）；~~License~~ 本轮实证已随 f2ab1a4 入库（MIT），此前多轮「License 催办」系沿袭性误报，予以纠正 | 无 | 累积提交（或按 W24-E~W28 拆五个）+ tag v0.7.0 + Release |
| 观察 | soak 36/36 收口汇总 | 巡检自动化自附判据已闭环，缺最终 36/36 分布汇总落档 | 今晚末轮采样后 | 收口报告：36 轮 PASS/FAIL 分布 + 归因结论（现有证据链已支持「本机负载×固定窗口」定性） |

## 第四部分：重构路线图

- **短期（1-2 周）**：①soak drain grace 改进 + 36/36 收口汇总落档（巡检脚本小改）；②cpp.py:161 注释订正（顺手清偿）；③rename 作用域增强 MVP（P0-2）；④用户提交 W14~W28 + 打版 0.7.0 + LICENSE。**验收**：全量 pytest ≥2351 绿、金标零回退、rename dogfood 扩样误拒率达标、soak 收口报告落 bench/results。
- **中期（1-2 月）**：①前端 /reports 历史版本展示 + rename 前端入口；②server /api/rename 补 language 参数；③extract-function 原语（rename 模式复制：plan/apply/all-or-nothing/dogfood 门全套）；④SECRET 家族注释行掩码预检（四语言同改）。
- **长期（3-6 月）**：①污点两级传播；②跨文件签名变更级联；③PR 式逐 hunk 审阅前端化；④偏好反馈环；⑤C++ 作用域增强（.h 纳入评估 + 模板浅支持）。

## 第五部分：风险提示

1. **soak 测量口径风险**（沿第八轮，定性已变）：FAIL 已归因为本机负载×固定窗口敏感项，但「固定 300s 窗口收尾即统计」的口径在其他部署环境（CI runner 共享负载）会复现同类假阳性——drain grace 是低成本根治，建议下波落地。
2. **技术债**：rename 作用域（P0-2）；结果版本化无清理策略；SECRET 家族注释行误报（全家族）。
3. **安全**：连续五轮无新安全发现（F6-R1 已清偿、脱敏链路闭环、/api/rename 治理链第一版全前置）——维持「安全治理已到位」定性；subprocess 沙箱资源限制缺失为已知常态。
4. **性能**：P0-1 第九次 429 持续阻塞；>50 万行单机内存未测；soak 显示单机审计时长对 CPU 负载敏感（空闲 <2.88s → 负载下数倍），多租户场景需资源隔离设计。
5. **合规**：**本轮纠正一个沿袭多轮的审计错误**——LICENSE 实为 MIT 且早已随 f2ab1a4 提交入库（`git log -- LICENSE` 实证），第八轮及之前报告的「License 连续 N 轮催办未补」不成立，此前轮次系互相沿袭未核实现场。合规侧真实待办只剩：W24-E~W28 改动提交 + push + 打版 0.7.0。

---

## 附：审计方法与证据清单

- 代码走读：`cli.py`（cmd_rename 互斥/dry-run/apply 段 + cmd_diff 的 seq 预检与 _history_version）、`server/app.py`（/api/rename 全段 + _ensure_source_allowed + 中间件覆盖）、`audit/refactor/rename.py`（language 校验段）、`audit/indexer/cpp_extract.py`（全文 232 行）、`audit/detect/rules/cpp.py`（全文 410 行：掩码状态机/SQL 双条件/SECRET 家族/LONG 作用域）、`audit/indexer/store.py`（cpp 跨文件解析段）、`audit/utils.py`（扩展名映射 .cpp/.cc/.hpp，.h 明示排除）、`tests/unit/server/test_server_rename.py`、`tests/unit/cli/test_cli_diff_seq.py`、`tests/integration/test_gray_release.py`（/api/rename 冻结断言）
- 实测：`python -m pytest tests -q`（2351 passed, 2 skipped, 346.77s）；`ruff check audit cli.py server bench`（All checks passed）；`python demo/run_demo.py`（verified 9.7s）；`python -m bench.run --offline --projects <10项目>`（P=1.000/R=0.844/F1=0.916 → audit_round9_goldset_20260919.md）；rename 终树七段实测（临时目录独立语料）；diff seq 端到端（真实 TaskStore 落库两版）；C++ 语料 E2E（6 命中逐位一致）；GLM 单点探针（HTTP 429，连续第九次）；soak 巡检档研读（33/36 采样 + 归因证据链）
- 审计过程记录：金标复跑首跑因 demo_proj 路径误用 mini_variant 得 R=0.437，经金标 project 字段核对纠正为 tests/samples/demo_proj 后与基线逐位一致（过程如实记档，报告文件已为正确结果覆盖）
- 按约束：未 commit、未 push、未修改任何源码（bench/results 新增 2 个报告文件 + 本报告）
