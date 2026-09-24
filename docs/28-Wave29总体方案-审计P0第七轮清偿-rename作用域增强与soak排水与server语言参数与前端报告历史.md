# Wave 29 总体方案：审计 P0 第七轮清偿（rename 作用域增强 / soak drain / server lang / 前端 reports）

> 依据：第九轮审计报告 `bench/results/audit_round9_20260919_复审.md`（74/100 B）P0 清单。
> 工作轮路由：第九轮审计（提示词一）完成 → 同对话工作轮（提示词二）。
> 执行方式：四卡并行后台代理 + 集成人统一联调（与 W23~W28 同款流程；并行窗口 4≤安全值 4）。

## 1. 范围裁决

| 审计 P0 项 | 本轮处置 | 理由 |
|---|---|---|
| P0-2 rename 作用域增强 | ✅ 卡 A | safe-rename 已变现，「多定义宁拒不改」的误拒率随采用量放大——能力深耕优先 |
| soak drain grace（第八轮起在案） | ✅ 卡 B | 归因已闭环（本机负载×固定窗口），drain 是低成本根治；顺带清偿 cpp.py:161 注释失真 |
| server /api/rename 补 language 参数 | ✅ 卡 C | CLI --lang 已有、server 缺——双入口对齐，半小时级 |
| 前端 /reports 历史版本展示 | ✅ 卡 D | W27-B 端点+W28-A CLI 消费面均已就绪，前端是最后一公里 |
| P0-1 成本对数 | ⏸ 跳过 | 09-19 探针仍 429（连续第九次），等配额窗口 |
| doctor 补 cpp | 集成人收口件 | 照 W27「doctor 补 go」先例，cli.py 不入任何卡 |
| ~~License 催办~~ | ✅ 已闭环 | 第九轮审计实证：MIT LICENSE 早已随 f2ab1a4 入库，此前多轮「License 催办」系沿袭性误报（审计链自我纠正） |

## 2. 卡片交付（文件所有权互不相交）

- **卡 A = P0-2 rename 作用域增强**：`audit/refactor/rename.py` + `tests/unit/refactor/`。口径「**作用域可分则分，分不清则拒**」：多定义点（n>1）不再一刀切拒绝，而是对每个替换点按上下文归属——①`self.X` 形态 → 归属调用点所在 class 词法作用域的同名 method（所在类无定义时沿 bases 名字匹配基类定义点，基类名不可解析 → 歧义）；②裸名 X（模块级代码/函数体内/`from m import X` 导入项）→ 归属模块级函数定义点（无模块级定义 → 歧义）；③`obj.X`/`cls.X` 等前缀形态 → 接收者类型不可静态判定 → **歧义点**。任一歧义点 → 整体拒绝（维持「宁拒不改」安全边界，errors 说明歧义位置）；全部可归属 → 按归属分组替换。单定义点路径行为逐字节零变化。CLI/server 零改动（消费 plan.errors 文案）。+15~20 用例（双类同名方法 self 调用分改/继承链/裸调用/from import/obj.X 拒绝/单定义回归）。dogfood 门：新语料含同名场景 verified ≥8/10。
- **卡 B = soak drain grace + cpp 注释订正**：`bench/stress/run_soak_long.py` + `audit/detect/rules/cpp.py` + 对应测试。drain 口径：300s 接纳窗口关闭后增加排水等待（`CODEAUDIT_SOAK_DRAIN_SEC`，默认 60，0=关）——窗口关时在途任务继续轮询至 drain 截止，drain 内完成 → 计 PASS 侧「drain 收编」；drain 后仍未终态才计 FAIL；报告新增 `drain_rescued`/`drain_still_running` 字段与归因行。**兼容约束**：巡检自动化（每 2h）可能正在调用本脚本——只加参数与统计字段，不改既有输出行格式与退出码语义（FAIL 判定条件只收紧不放松）。cpp.py:161 行内注释订正为与 139 行实现一致（「未闭合按行尾截断回 code」）。+4~6 用例。
- **卡 C = server /api/rename language 参数**：`server/app.py` + `tests/unit/server/test_server_rename.py`。RenameRequest 加 `language: str = "python"`（缺省零变化），透传 plan_rename；非法语言走既有 plan.ok=False → 400 中文 detail 路径（端点零新增分支）。+3 用例（缺省 python 零变化/透传非法值 400/合法值透传）。gray_release 冻结断言零变化（路由不变）。
- **卡 D = 前端 /reports 历史版本展示**：`frontend/src/`（api/client.ts + api/types.ts + pages/TaskDetail + 测试）。TaskDetail 加「报告历史」面板：调 `GET /api/audits/{id}/reports`（摘要列表）展示版本号/时间/健康分，点开版本显示该 seq 报告关键摘要（`GET /api/audits/{id}/reports/{seq}`），无历史时面板显示「暂无历史版本」。验收：tsc + vitest + build 全绿，新增用例 ≥4（列表渲染/空态/版本展开/接口失败降级）。

## 3. 集成人收口件（集成人自做，不派卡）

- `cli.py` `_DOCTOR_LANGS` 补 cpp（清偿 W28 已知边界，照 go 先例）+ doctor 测试同步（token 断言加「语言包 cpp」+ 可选级警告用例扩 cpp）。**定向 16 passed + ruff 绿**。
- docs/28（本文档）+ CHANGELOG Wave 29 段 + 记忆同步。
- rules.md 不再生（本轮零规则数变化，92 条维持）。

## 4. 联调与验收记录（2026-09-19 实测）

**逐卡验收（全部通过）**：
- 卡 A：`tests/unit/refactor + test_cli_rename + test_server_rename` 合计 **106 passed**（含新增 17 例），ruff 绿；dogfood 三场景实测（双类同名 self 分归属 / obj.X 拒绝含位置明细 / 跨文件继承链放行）；单定义 15 个既有用例零改动语义通过。
- 卡 B：`test_soak_drain（11）+ test_rules_cpp（30）` 合计 **41 passed**，ruff 绿（顺手修一处 B905）；`--drain-sec` 冒烟可见；外部消费者（memdiag run_serverdiff）导入验证通过。
- 卡 C：`test_server_rename（12=9+3）+ gray_release（4）` 合计 **16 passed**，ruff 绿。
- 卡 D：tsc 零错误、vitest **76 passed**（含新增 6）、build 绿；自报中途 1 处 testing-library 文本匹配问题已自修复绿。

**统一联调（零集成冲突）**：
- 全量 pytest **2382 passed, 2 skipped**（428.50s）= 基线 2351 + 净新增 31（卡A 17 / 卡B 11 / 卡C 3）逐位对账；期间零并行手动命令。
- ruff 全域（audit/cli/server/bench）All checks passed；demo 端到端 **verified**（fix verified=1）。
- 金标一条命令 **P=1.000 / R=0.844 / F1=0.916、333 报告**（`w29_goldset_20260919.md`）——零回退门连续九轮确认。
- 压测：soak 300s + drain 60 新口径首验 **总判定 PASS**（49 任务终态率 100%、零 5xx、RSS 稳态斜率 0.715 MB/min、金丝雀三重断言过；「排水归因」行正常渲染，本窗任务全在窗内完成属空载路径）→ `soak_w29_drain首验.md`。
- 前端三件套（tsc/vitest/build）绿（卡 D 申报 + 集成人 vitest 复核 76/76）。
- P0-1：09-19 探针仍 429（连续第九次），w22_focus_compare 对数继续阻塞，如实记档。

**卡 A 申报的跨卡协调点（集成人裁决）**：dogfood Validator 语料从「多定义反例」演进为「歧义反例」（补 `models.Validator` 交叉引用使其在新口径下保持拒绝）——CLI/server 的 rename 所有权外测试以该语料锁「多定义→拒绝」管线，卡 A 通过补自然引用而非改他人测试来保持兼容，裁决=接受（正向「可归属放行」由卡 A 自有 17 用例锁定）。

**已知边界（如实申报）**：
1. 卡 A：同名参数/局部变量撞多定义点方法名时保守误拒（绑定与引用不区分）；`super().X` 一律歧义不追 super 链；基类只看 bases 一层；接收者类型静态推断不做。
2. 卡 B：未执行真实 72h 长跑端到端（真实巡检由自动化下次执行自然验证）；窗口末尾 1-2s 提交的任务原上限晚于窗口关闭+drain 时不额外延长（drain 语义是「补 60s」非「无限等」）；drain 开启时 wall_total 最多延长 drain_sec。
3. 卡 C：language 合法值当前仅 python（与模块 MVP 口径一致）。
4. 卡 D：面板在 queued/running/failed 态也展示（无版本时空态）——依据 server 端点注释「不要求 done 态」；若产品上只想 done 态展示为一行改动。

## 5. 下一轮入口

- 收口后按工作流：**下轮=待用户新开 GLM-5.3 对话贴提示词一（第十轮审计）**，复审重点=①四卡新增面（rename 归属判定的歧义边界、drain 口径的 rate 只升不降论证、前端面板的降级路径）；②P0-1 第十次探针；③soak 36/36 收口汇总（自动化承接）+ drain 上线后巡检 FAIL 是否消失的对照观察；④W24-E~W29 积压催办（提交+push+打版 0.7.0；~~License~~ 已实证 MIT 随 f2ab1a4 入库，此前多轮催办系沿袭性误报）。
