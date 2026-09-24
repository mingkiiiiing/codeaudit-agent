# Wave 27 总体方案：审计 P0 第五轮清偿（F7-R1 根治 / 结果版本化 / safe-rename 原语）

> 依据：第七轮审计报告 `bench/results/audit_round7_20260919_复审.md`（73/100 B）P0 清单。
> 工作轮路由：第七轮审计（提示词一）完成 → 同对话工作轮（提示词二）。
> 执行方式：三卡并行后台代理 + 集成人统一联调（与 W23~W26 同款流程）。

## 1. 范围裁决

| 审计 P0 项 | 本轮处置 | 理由 |
|---|---|---|
| P0-12 F7-R1（from_env 二次调用丢 .env 值） | ✅ 卡 A 方案 b | 根治双入口；方案 a 经依赖分析弃做（见 §2） |
| 结果版本化（P2 遗留，连续四轮在案） | ✅ 卡 B | report_history 表 + 查询面，diff 消费接线归后续 |
| safe-rename 确定性原语 MVP（代际缺口之首，连续五轮在案） | ✅ 卡 C | 纯模块 + dogfood 20/20 门；CLI/server 接线归后续 |
| P0-1 成本对数 | ⏸ 跳过 | 09-19 探针仍 429（连续第五次），等配额窗口 |
| C++ 语言包 / doctor 已由卡 A 顺带补 go | doctor ✅ / C++ 顺延 | doctor 是 W26 遗留小项，本轮顺带清偿 |

## 2. 卡片交付（文件所有权互不相交）

- **卡 A = P0-12**：`audit/config.py` `_load_dotenv` 增加模块级 `_DOTENV_CACHE`——首次成功加载后缓存解析结果，`_ENV_LOADED` 置位后的调用返回**浅拷贝**（防调用方污染缓存）；缺文件/不可读不置标志不写缓存的既有语义保留。「加载一次」防重复 IO 与「GLM_* 键不写 os.environ」防污染设计均不变。**方案 a（cmd_resume 调序）经依赖分析弃做**：①读任务行需先有 store，store 定位需 work_root，单 store 无法成立；②`from_env()` 无 overrides 时 work_root 恒为字段默认值 `".codeaudit"`，而 `config.work_root` 来自 config_json（带过 --work-root 的任务落库即不一致）——改用 config.work_root 定位 db 会把落库写错库，行为明确改变；b 落地后 583 行前置 from_env 已无害。resume 摘要新增 `LLM 通道：已启用 / 未启用（未检测到 API Key，本次为纯规则模式）` 防御性标注；`_DOCTOR_LANGS` 补 go（W26 遗留清偿）。+4 新用例、5 处既有断言更新（F7 语义演进，报备）。
- **卡 B = 结果版本化**：`audit/taskstore.py` 新增 `report_history` 表（PK(audit_id,seq)，CREATE TABLE IF NOT EXISTS 随构造建立，老库零破坏）；`set_report` 重写为 BEGIN IMMEDIATE 单事务「UPDATE 当前 + INSERT 历史」双写（seq 标量子查询 COALESCE(MAX)+1，照 append_event 原子形态）；`get_report(seq=)` 可选参数（缺省路径零变化）+ `list_reports` 摘要面；delete/prune 级联清除历史（同 events 语义，保证同 ID 重建 seq 归 1）。server 新增 `GET /api/audits/{id}/reports`（摘要列表）与 `GET /api/audits/{id}/reports/{seq}`（版本 JSON，404=任务或版本不存在）；gray_release 路由冻结断言同步。+16 用例（含并发 set_report seq 连续 1..24）。
- **卡 C = safe-rename MVP**：`audit/refactor/rename.py` 纯模块（零 LLM）——`plan_rename`（定义点复用 py_extract Symbol 口径 + 引用点自有 tree-sitter 遍历；token 级 identifier 节点字节区间替换，**物理上不可能越界到字符串/注释**——二者子树无 identifier 节点；import 模块路径组件跳过；非法名/old==new/多定义点（宁拒不改）/解析失败计划期即拒）+ `apply_rename`（dry_run 默认；写前「内容一致性 + AST 复检」双校验，任一失败 all-or-nothing 不落盘）。**dogfood 实测 20/20 verified**（门 ≥18：函数/方法/类/常量/跨文件引用，逐文件 AST parse + 旧名零残留 + 新旧 token 数对账 + 幂等）。开发期自纠 1 处真缺陷（多定义拒绝时补丁未清空）。+16 用例。

## 3. 集成人联调记录

- 改动面核对：三卡文件与申报逐项吻合；卡 A 方案 a 弃做决策经集成人复核接受（循环依赖 + 落错库风险论证成立，b 已根治 key 问题）。
- 统一联调终验（最终树）：
  - 全量 pytest **2277 passed, 2 skipped**（339.68s）＝基线 2239 + 净新增 38（卡A 4 / 卡B 16 / 卡C 16 + 契约用例随语义演进更新）；**联调发现并修复 1 处集成冲突**：`tests/unit/contract/test_workspace_config.py::test_config_from_env` 钉住的正是 F7-R1 旧有损语义（delenv 后断言不可用），方案 b 落地后仓库真实 .env 经缓存可见而失封闭——修复为确定性真契约（清缓存标志 + chdir 无 .env 目录，环境与 .env 双无 → 不可用）。首跑 1 failed / 2276 passed → 修复后重跑全绿；
  - ruff 全域绿；demo 闭环 **verified 9.0s**；
  - 金标一条命令复跑 **P=1.000 / R=0.8444 / F1=0.916**（240 条，`w27_goldset_20260919.md`）——零回退连续六轮；
  - rename dogfood 终树复核 **70 passed（20/20 verified）**；
  - P0-1：09-19 探针仍 429（连续第六次），w22_focus_compare 对数继续阻塞，如实记档；
  - soak：引用 72h 巡检记录（审计期 28/36 全 PASS）与 W26 收口四项 PASS 实测，本轮未重跑（联调纪律：pytest 期间零并行写 .codeaudit 命令，并避开 02:07 巡检开火窗——W26 教训落实）。
- P0-1：09-19 探针仍 429（连续第五次），w22_focus_compare 对数继续阻塞，如实记档。
- 联调纪律执行：全量 pytest 期间零并行手动命令，并避开 02:07 soak 巡检开火窗口（W26 hygiene 教训落实）。

## 4. 已知边界（如实申报）

1. 卡 A：`cli.py:583` 前置 from_env 保留（冗余但无害）；若未来想让无 --work-root 的 db 定位跟随 config_json.work_root，需重设计 store 开闭顺序（另开卡）。
2. 卡 B：`codeaudit diff` 未接历史 seq（消费接线归后续轮）；历史无清理/配额策略；前端 /reports 展示接线未做。
3. 卡 C：safe-rename MVP 边界——同名不同作用域不区分（多定义点宁拒不改）、动态引用（getattr/反射）不动、import 重导出/`__all__` 联动不做、仅 python；CLI/server 接线归后续轮。

## 5. 下一轮入口

- **下轮路由=待用户新开 GLM-5.3 对话贴提示词一（第八轮审计）**，复审重点：①三卡新增面（_DOTENV_CACHE 缓存语义与浅拷贝防污染、report_history 并发 seq 与级联、rename token 替换的越界防护论证）；②P0-1 第六次探针；③soak 36/36 收口结论；④W14~W27 积压催办（提交+push+打版 0.7.0+License）。
- 工作轮候选：①P0-1（等窗口）；②safe-rename CLI/server 接线（`codeaudit rename` 子命令）；③diff 接历史 seq；④C++ 语言包评估。
