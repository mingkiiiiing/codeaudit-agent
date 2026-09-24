# Wave 30 总体方案：审计 P0 第八轮清偿（污点传播 MVP / rename 类型推断 / SECRET 注释掩码 / 前端 rename 工具）

> 工作轮路由说明：上轮已收口「第九轮审计 + W29 工作」完整循环；本轮为用户指令直接触发的**连续工作轮（W30）**，任务来源=第九轮审计中期路线图遗留项 + W29 申报边界 + 家族级观察项。
> 执行方式：四卡并行后台代理 + 集成人统一联调（与 W23~W29 同款流程；并行窗口 4）。

## 1. 范围裁决

| 来源项 | 本轮处置 | 理由 |
|---|---|---|
| P0-1 成本对数 | ⏸ 跳过 | 09-19 探针仍 429（**连续第十次**），等配额窗口 |
| 存量-A 污点传播（第九轮审计 P0 清单） | ✅ 卡 A | 「数据流级安全」代际缺口头项：现有 SQL/命令注入规则均为单行形态判定，变量经赋值链传播即漏 |
| rename 类型推断（W29-A 申报边界：obj.X 一律歧义/super().X 不追/基类一层） | ✅ 卡 B | W29 落地的「宁拒不改」误拒率大头即 obj.X 形态——直接构造赋值可静态判定，属确定性增强 |
| SECRET 家族注释行误报（第九轮审计家族级观察项） | ✅ 卡 C | 四语言同改一个小掩码预检，误报面收敛 |
| rename 前端入口缺失（第九轮审计 UX 未达面） | ✅ 卡 D | CLI+API 双入口已有，前端是能力变现最后一公里 |
| soak 36/36 收口 | 巡检自动化承接 | drain 已上线（W29），FAIL 应消失——收口后做对照结论 |

## 2. 卡片交付（文件所有权互不相交）

- **卡 A = 污点传播 MVP（PY-TAINT-UNSAFE-SINK）**：新文件 `audit/detect/rules/py_taint.py` + `tests/unit/detect/` + 语料。口径：**python 单语言、函数内、单文件、两级传播**（AST-only，tree=None 不产命中，沿 PY-NONE-DEREF 先例）。source 集（保守固定形态）：`request.args/form/values/json/cookies/headers` 的 get/索引、`request.get_json()`、`input()`、`sys.argv` 元素；传播：函数体内赋值链（右值含污染名的 Name/f-string/`.format()`/`+` 拼接/容器构造 → 左值入污染集，可多级）；sink 集：`.execute(...)`/`.executemany(...)`、`eval`/`exec`、`os.system`/`os.popen`、`subprocess.run/call/Popen/check_output`（shell=True 或含污点实参）。命中报 sink 行 + evidence 传播链（source 行→…→sink 行），severity high/security。规则库 92→**93 条**（registry+rules.md 由集成人再生）。**验收**：自建 taint 语料正例 ≥20 检出 ≥15、干净对照 ≥10 零误报、AST 佐证口径与既有规则一致、tree=None 不产命中用例。
- **卡 B = rename 类型推断增强**：`audit/refactor/rename.py` + `tests/unit/refactor/`。三项放宽（歧义→可归属）：①`obj.X`：同文件内 `obj = ClassName(...)` **直接类名构造赋值**（仅 Name = ClassName(...) 单层形态）→ 归属 ClassName 的 method 定义点（构造类名不在定义点集 → 仍歧义）；②`super().X` → 所在类 bases 解析（同 self 口径）；③基类链**传递闭包**（限扫描集内可见的基类定义，递归解析，环防护）。仍未判定 → 歧义拒绝（安全边界不变）。**硬约束**：W29 的 17 用例与既有全部用例零改动通过（obj.X 用例语料均无直接构造赋值，放宽不触雷）；docstring 边界段同步（别名导入构造/工厂函数返回值/跨文件构造不做）。+10~14 用例。
- **卡 C = SECRET 家族注释行掩码预检（四语言同改）**：`audit/detect/rules/python.py`、`java.py`、`go.py`、`cpp.py` + 各测试。口径：SECRET 规则在 raw 行匹配 `_ASSIGN_RE` 成功后，**同正则在 masked 行复验**——masked 后不再匹配 ⇒ 该行赋值形态处于注释内（字符串内容被 mask 但引号定界符保留，真实赋值行的 `NAME = "` 形态在 masked 行仍匹配）⇒ 跳过不报。四语言 SECRET 各 +2~3 用例（行注释内不报/块注释中间行不报/真实赋值含敏感值仍报/测试夹具假密钥字符串不受影响）。规则判定口径零变化（R1-9/R4-1/R4-7 闸门不动，只加注释排除）。
- **卡 D = 前端 rename 工具面板**：`frontend/src/`（新组件 + 路由/入口 + client.ts + types.ts + 测试）。功能：表单（source_path/old_name/new_name）→ `POST /api/rename`（apply=false）→ unified diff 展示 → 「确认应用」二次按钮 → apply=true → 结果摘要；400/401/409 错误中文文案展示（409 显示 all-or-nothing 未落盘说明）。挂载位置由卡 D 依前端布局自选最自然处（Dashboard 工具区或独立路由），申报即可。**dry-run 默认 + 显式确认**交互必须与 CLI 同构。测试 ≥5（表单提交 dry-run diff 渲染/确认应用/400 错误展示/409 展示/空表单禁用）；tsc/vitest/build 绿。

## 3. 集成人收口件（集成人自做，不派卡）

- registry 计数断言补 W30 段（92→93）+ docs-site/rules.md 再生（93 条）。✅ 已完成（2026-09-21，见 §4）
- P0-1 第十次探针已做（429，docs/29 如实记档）。✅（第十一次探针定性更新见 §4）
- docs/29（本文档）+ CHANGELOG Wave 30 段 + 记忆同步。✅ 已完成（2026-09-21）

## 4. 联调与验收记录（2026-09-21 实测）

> **收口背景（如实记档）**：W30 开发会话完成四卡代码与测试后**中断于集成接线之前**——`registry.py:15` 已 import `build_py_taint_rules` 但无 `register_all` 调用，CHANGELOG/docs/记忆均未同步。第十轮审计（2026-09-21，`bench/results/audit_round10_20260921_复审.md`，74/100 B）将该状态定性地为 **F10-R1（P1）taint 未接线主链路**（registry 实测 92 条非方案声称 93；rules_for("python") 无 TAINT；CLI/server/CI/前端四入口不可达；三重证据=动态计数+ruff F401+CI 看门测试 `test_ruff_check_zero_errors` 如实红——全量 pytest 首次带红）与 **F10-R2（P2）收口件三不同步**，立 P0-13 清偿卡。本节为 P0-13 执行后的完整实测记录。

**逐卡验收（全部通过）**

- 卡 A taint：`test_py_taint.py` 定向 **101 passed**（24 正例逐条精确断言 sink 行 + 12 对照零误报 + tree=None 契约 + 同名覆盖，另含 P0-13 新增接线断言）；E2E 差分实证：规则直跑污点语料命中 1（sink 行），接线前 rules_for 不可达、接线后可达。
- 卡 B rename 三放宽：`test_rename_typeinfer.py` 13 passed（W29 17 用例零改动硬约束达成）；审计期 E2E 五场景——构造赋值放行（2 替换点）/super().X 基类链放行/两层基类链传递闭包放行/参数传入歧义仍拒（含 file:line 明细）/继承环 A→B→A 不挂死按不可解析拒绝。
- 卡 C SECRET 掩码：四语言（python.py:1461 / java.py:385 / go.py:381 / cpp.py:391 `_is_comment_only`）行为实证——注释假声明不报、真实赋值仍报；`test_secret_comment_mask.py` 全绿。
- 卡 D 前端：tsc 0 错、vitest **83 passed**（14 文件，含 RenameTool）、build 绿；两段式交互与 /rename 路由挂载核对。

**统一联调（P0-13 收口后终验）**

- 全量 pytest **2518 passed + 2 skipped**（W29 基线 2382 + W30 四卡净增 134 + 接线断言/守卫 2，ruff 看门回绿）；ruff 全域 **0 错**；demo 闭环 **verified**（21.5s）。
- 金标一条命令 **P=1.000 / R=0.844 / F1=0.916、报告 333 条**（`bench/results/w30_goldset_20260921.md`）——**零回退门连续十一轮**；taint 上线金标零污染（预判依据：金标 240 条全 python 项目但 taint source 面 0 处，实测复验成立）。
- rules.md 再生 93 条（`python scripts/gen_rule_docs.py`，总表 Python 52→53、统计同步）；README 规则数 82→93 顺带收口（W21 以来的存量漂移，含六语言口径）。
- **接线完整性守卫**：新增 `tests/unit/detect/test_registry_wiring.py`——pkgutil 遍历 rules 包树全部 `build_*_rules` 工厂（当前 20 个），任何工厂产出的规则 id 未在 DEFAULT_REGISTRY 即红；**变异验证**通过（屏蔽 taint 注册行→守卫红 1 failed，恢复→绿）——「import 了但没注册」的 F10-R1 形态从此在接入期被抓，不再依赖 ruff 事后红。
- soak 36/36 收口总汇（第九轮审计期收官，`bench/results/soak_long_w21_巡检.md`）：**29 PASS / 7 FAIL / 0 崩溃**，1332 任务零 5xx 零丢失；7 FAIL 归因闭环=本机 fuwai 工作负载×固定 300s 窗口测量敏感项（负载消退末轮即恢复 PASS；RSS 跨轮平台零爬升=无泄漏）；drain grace（W29-B）即该归因的改进落地，后续长稳由巡检自然对照。
- **P0-1 第十一次探针（09-21）定性更新**：非 429，而是 **ConnectError**（open.bigmodel.cn TLS UNEXPECTED_EOF，网络层不可达，httpx 直连复现）——待网络环境核实恢复后按 docs/21 §5 对数。

**已知边界（如实申报）**

- taint：跨函数/跨文件不追、别名导入 sink 不识别、属性链中间污染不传播、分支路径不敏感（线性源码序保守口径）、`super(C, self).X` 带参不认——均 docstring 申报，宁漏报不误报。
- rename：别名构造/工厂返回值/跨文件构造赋值回溯不做；同名参数撞名保守误拒；嵌套类 bases 仅尾段字面比对。
- rules.md「按语言」统计仍不含 go/cpp（`gen_rule_docs.py` LANG_ORDER 未扩，W28 遗留失真，本轮未动——留待文档轮顺带）。
- doctor 语言探测 / rules.md 总表 / gen_rule_docs 的 LANG_ORDER 三者口径不一的存量项，随打版前文档统一收口。

## 5. 下一轮入口

- P0-13 已清偿（本节）。按工作流：**下轮=工作轮或审计轮由用户路由**——审计轮复审重点=本节实测数据抽核 + 守卫测试有效性 + README/六语言口径一致性；工作轮候选=①P0-1 对数（等网络/配额窗口）②rules.md LANG_ORDER 扩 go/cpp③rename 参数绑定区分等 W29/W30 申报边界；④**W24-E~W30 积压催办（提交+push+打版 0.7.0——当前工作区已全绿，具备提交条件）**。
