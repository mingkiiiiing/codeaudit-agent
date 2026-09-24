# 更新日志

本项目所有显著变更记录在此文件。

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

Wave 13（CI 在线评估、Docker 沙箱后端、多 worker 压测基线、前端降级展示）、Wave 14（全方位审查清偿：报告安全、沙箱可靠性、服务端运维、文档一致性）、Wave 15（全库四维审计清偿：服务端安全治理、多 worker 存储一致性、依赖与 CI 自举、规则族收敛、重构执行闭环、测试盲区补齐）与 Wave 16（专项验收短板清偿：依赖与配置安全扫描、克隆与死代码检测、重构方案分级、分层架构与命名/PII 规则补缺）。API 契约形状零变化（新增治理能力均为 opt-in，env 缺省 = 既有行为）；W13/W14/W15 方案与收口记录分别见 docs/18、docs/19、docs/20。

### Added（Wave 30 审计 P0 第八轮清偿：污点传播 MVP / rename 类型推断 / SECRET 注释掩码 / 前端 rename 工具，docs/29）

- **W30-A 污点传播 MVP（`PY-TAINT-UNSAFE-SINK`，「数据流级安全」代际缺口头项）**：新规则 `audit/detect/rules/py_taint.py`——python 单语言、函数内、保守固定形态（source=`request.args/form/values/cookies/headers` 取值 / `get_json` / `input()` / `sys.argv`；sink=`.execute/.executemany`（仅查 SQL 首实参，参数化查询不误报）/ `eval/exec` / `os.system/popen` / `subprocess` 家族），函数体内赋值链多级传播，命中报 sink 行 + evidence 传播链；AST-only（tree=None 不产命中，沿 PY-NONE-DEREF 先例），边界（跨函数/别名导入/属性链中间污染等）docstring 如实申报。
- **W30-B rename 类型推断三放宽（W29-A「宁拒不改」误拒面清偿，安全边界不变）**：①`obj.X`——引用点所在函数体内向上回溯最近一次 `obj = ClassName(...)` 直接类名构造赋值（仅 Name 左值 + 裸类名单层形态，别名/工厂/方法链不做）；②`super().X`——沿所在类基类链解析（零参 super 才认）；③基类链传递闭包——递归解析限扫描集内可见基类，同名类多定义剔除、继承环按不可解析拒绝（visited 防护不挂死）。仍未判定仍歧义整体拒绝；W29 全部既有用例零改动通过。
- **W30-C SECRET 家族注释行掩码预检（四语言同改，第九轮审计家族级观察项清偿）**：python/java/go/cpp 四语言 SECRET 规则在 raw 行命中赋值形态后，同正则在 masked 行复验——masked 不再匹配 ⇒ 注释/三引号内容中的假声明 ⇒ 跳过；真实赋值行 `NAME = "` 结构在 masked 上保留不受影响，判定闸门（熵/词表）零变化。
- **W30-D 前端 rename 工具面板（safe-rename 能力变现最后一公里）**：`/rename` 路由 + RenameTool 组件——两段式强制（先 dry-run 预览 diff，后显式确认应用）、表单变更即失效旧预览（防 stale 竞态）、400/401 服务端中文 detail、409 附 all-or-nothing 未落盘说明。
- **集成收口（P0-13，第十轮审计 F10-R1/F10-R2 清偿）**：W30 开发会话中断于接线前——`registry.py` 已 import `build_py_taint_rules` 但漏 `register_all`，主链路 92≠93 四入口不可达（全量 pytest 首次带红）。收口补齐：registry 接线（92→**93 条**）、5 处计数断言同步、`test_py_taint` 接线断言、rules.md 再生 93 条、**接线完整性守卫测试** `test_registry_wiring.py`（pkgutil 遍历 rules 包全部 `build_*_rules` 工厂，任何「import 了但没注册」在接入期即红；经变异验证真实检出 F10-R1 形态）、README 规则数 82→93 存量漂移顺带收口。
- 联调与验收：全量 pytest **2518 passed + 2 skipped**（含 W30 四卡 134 用例 + 收口断言/守卫）、ruff 全域 0 错、demo verified、金标一条命令 **P=1.000 / R=0.844 / F1=0.916**（`w30_goldset_20260921.md`，**零回退门连续十一轮**——taint 上线金标零污染实测）、前端 tsc/vitest 83/build 绿、doctor 10OK。P0-1 对数维持阻塞（09-21 探针定性更新：open.bigmodel.cn 网络层不可达 ConnectError，非 429，待网络恢复后按 docs/21 §5 对数）。

### Added（Wave 23 审计 P0 第二轮清偿，docs/22）

- **W23-A apply-to-source 预览确认回路（P0-2）**：`codeaudit apply <audit_id> [--patch N] [--all-verified] [--yes]` 把审计补丁应用回源码——**默认 dry-run 预览不落盘**，`--yes` 才写入；服务端新增 `POST /api/audits/{id}/patches/{n}/apply`（语义与 CLI 一致，gray_release 路由冻结断言同步）。原文指纹（Patch.target_sha256 生成侧补记）：目标文件被审计后手改过（sha256 不一致）→ 拒绝该 patch；任何拒绝整体不落盘（all-or-nothing）；老报告无指纹可预览不可落盘（诚实降级）。
- **W23-B AST 断供修复（P0-3）**：python 文件在 build_rule_contexts 与并行 worker 同口径经 tree-sitter 解析传入 `ctx.tree`（同源码→同树→同命中）；失败/超时/无解析器/`CODEAUDIT_DISABLE_AST=1` 降级 None 绝不阻断，统计写 `extra["ast_wiring"]`。SQL 注入/并发 2 规则/ORM N+1 三规则 AST 佐证只提升置信（`meta["ast_confirmed"]`，confidence 0.7→0.8），命中行号集合与无 AST 完全一致（金标零回退实测达成）。新规则 `PY-NONE-DEREF`（bug/high，AST-only：同函数 None 赋值→解引用；tree=None 不产命中），静态规则库 82 → **83 条**。
- **W23-C 测试覆盖盲区识别（P0-5）**：报告新增 `untested_hotspots` 节——critical/high 命中的非测试文件公开符号经索引 call_graph callers 反查零测试触达即盲区，输出 TOP 20 + 占比 + methodology（动态调用偏悲观、resolved_ratio 偏低放大误差等局限随节诚实标注，仅供测试补充排期参考）。`AuditReport.untested_hotspots` 为带默认值可选字段（旧报告兼容，契约 schema 不破坏）。
- **W23-D commit message / PR 描述生成（P0-8）**：`apply` 命令新增 `--commit-message` / `--pr-description`（纯字符串拼接、零 LLM token）；单补丁模板 `fix(<category>): <title> (<rule_id>)`、多补丁去重；rule_id 经 Issue.evidence 的 `rule:XXX` 证据行反查（与 SARIF 导出同款约定，契约零变化），反查不到降级 `fix(general): … (unknown-rule)`；PR 描述含逐补丁验证状态表（tests_run=0 显式标"未运行"、compat_notes 非空才列）；dry-run 输出带「（预览）」标注。
- 联调与验收：四卡新增 106 用例、全量 pytest 2036 passed（基线 1930）、ruff 绿、demo 闭环 verified、金标 10 项目 240 标签 P=1.000 / R=0.844 / F1=0.916（与基线逐位一致）、gray_release 集成含 apply 端点断言 4 passed。P0-1/P0-7（成本三项翻默认、模型路由 fallback）维持阻塞于 GLM 账户余额（HTTP 429），充值后按 docs/21 §5 判据对数。

### Added（Wave 24 Java 语言包 / resume 断点续跑 / 体验快赢，docs/23）

- **W24-A Java 语言包（P0-4）**：`tree-sitter-java` 接入——`SUPPORTED_LANGUAGES` 3 → **4 语言**，maven/gradle/src 根 FQN 推导、import 三形态（普通/static/通配）别名解析、变量声明类型推断调用点解析（语料 resolved_ratio 实测 **0.517 ≥ 0.4**）；新规则 `JAVA-SQL-INJECTION`（critical）/`JAVA-HARDCODED-SECRET`（critical，复用 python 熵闸门口径）/`JAVA-LONG-FUNCTION`（style/medium，>80 行），AST 佐证走 W23 既有 `confirm_hit` 通路且 engine `_AST_LANGUAGES` 扩 java（js/ts 维持行级）；13 文件语料端到端实测三规则命中 8 处、8 反例零误报、AST 解析生效率 100%；金标存量 10 项目逐位零回退（双门断言口径落地）。
- **W24-C resume 断点续跑 + 报告对比（P0-6）**：`audits.stage_done` 列（老库 ALTER 自动迁移）+ 阶段完成即发结构化 `stage_done` 事件（同一事务幂等并入列，事件可见⇔进度可见）；重启 sweep 分流——running 且带进度 → **interrupted**（工作副本/索引保留、count_active 排除、prune 守卫拒绝淘汰），无进度 → failed（既有语义逐字节保留）；pipeline `resume_stages` 恢复参数：ingest 跳过重拷、index 仅重开既有库不重建（重开失败回落全量）；`codeaudit diff <A> <B>` 报告对比三栏（fixed/new/persisted，(file, rule, 行号±3) 匹配，audit_id / report.json 路径双支持）。
- **W24-B 体验三件（P1-快赢，第三轮起遗留整批清偿）**：①`verify_stats` + `ast_wiring` 进报告「检测质量观测」节（verify 四态表 + AST 解析生效率，有则渲染无则省略，JSON 通道零新字段）；②CLI 实时进度——`cmd_run` 逐阶段输出 `[stage n/7]` 到 stderr（`--check` 门禁 stdout 纯净契约不破，`--quiet` 可关）；③`codeaudit doctor`（零 Key 环境自检：Python/git/语言包/.env/配置/Docker 八类，失败退出 1、仅警告退出 0，键值绝不回显）+ `codeaudit init`（.codeaudit.toml 注释模板骨架，已存在拒绝覆盖）。
- **apply P2 加固（第四轮审计清偿）**：symlink 目标规划期+写入期双重拒绝（防 os.replace 静默把链接替换为普通文件）；生成侧多补丁同文件指纹链顺序语义显式化（docstring 声明勿并行化）。
- **W24-E resume 扩展（detect 可续跑 + CLI 接线）**：detect 产物（issues + stats 快照）落盘 `state/detect.json`（尽力写入绝不阻断），`_RESUMABLE_STAGES` 扩 detect——resume 时最贵的规则扫描 + LLM 审查通道可整体跳过（两道降级防线：schema 校验剔除 / 加载失败回落全量重跑）；新增 `codeaudit resume <audit_id>`（taskstore `mark_resuming` 仅 interrupted / failed+进度 复位，config_json 重建剔除脱敏 api_key 由 .env/进程环境补全，再断自动回 interrupted 可再次续跑）。+12 测试项。
- 联调与验收：三卡新增 105 用例 + 集成人接线四件（engine java AST / cli diff / 规则手册 java 语言表 86 条 / apply P2）；联调发现并修复 5 处集成冲突（同一根因：stage_done 事件补 `message` 键满足事件协议不变式）；全量 pytest **2140 passed**（基线 2036 + 净新增 104）、ruff 全域绿、demo 闭环 verified（10.0s）、金标 **P=1.000 / R=0.844 / F1=0.916 与基线逐位一致**。P0-1/P0-7 维持阻塞于 GLM 账户余额（HTTP 429）。

### Added（Wave 25 审计 P0 第三轮清偿：口径固化 / 模型 fallback / resume 全接线，docs/24）

- **W25-A bench 官方口径固化（P0-10，第五轮审计 F5-R1 清偿）**：`python -m bench.run` 的 `--goldset` 默认值改为仓库根锚定的 `bench/datasets/goldset.jsonl`（官方 240 条，任意 CWD 正确），新增 `--offline` 开关（`api_key=""` 强制离线，同时透传消融 `base_overrides` 防 `--ablation --offline` 混入 LLM 通道）；README 新增「指标复现」节——一条命令直出官方 P=1.000/R=0.8444/F1=0.9157。修复前口径坑：默认仅加载 demo_proj 12 条金标且不强制离线，.env 有 key 时复现出 P=0.123 伪回归（第五轮审计首跑实测踩中）。+4 用例。
- **W25-B 模型路由与 fallback（P0-7）**：新增 `audit/llm/router.py` `RouterClient`——主模型（`config.model`）LLMError（重试耗尽）后自动切换备用模型重试一次（`GLM_MODEL_FALLBACK` env / `AuditConfig.fallback_model`，**默认空 = 零行为变化**），双败上抛并保留异常链，`fallback_used` 计数与主备统计求和透出；主备独立 GlmClient 实例（缓存键天然隔离、共享连接池）；ConfigError 不触发切换。429 单点实证（第五轮探针）的架构面清偿——对数判据（P0-1）仍等配额窗口。+10 用例。
- **W25-C resume 全接线（P0-9，第五轮审计 F5-R2 清偿）**：①CLI 自愈——`codeaudit resume` 对 running 滞留任务（SIGKILL 级硬杀）先 `sweep_interrupted(grace_seconds=CODEAUDIT_SWEEP_GRACE_SEC，缺省 30)` 再重读：已扫为 interrupted 则续跑，仍 running（窗口内有活动）则中文报错退出 1；既有「不可续跑」报错与退出码逐字节不变。②服务端新增 `POST /api/audits/{audit_id}/resume`（守卫与 CLI 共用 `_resume_guard_action` 纯函数；404/409 语义；复用 `_launch_audit` 后台执行与事件流；gray_release 路由冻结断言同步）。③真实进程 E2E：滞留 running 任务 → CLI resume 自愈 → 续跑 done（跳过 ingest，问题数与语料金标命中交叉一致）。+13 用例（CLI 3 + server 7 + 冻结断言等）。
- 联调与验收：三卡并行 + 统一联调——全量 pytest **2176 passed**（基线 2152 + 净新增 24）、ruff 全域绿（含 bench）、demo 闭环 verified（8.4s）、金标终树复跑 **P=1.000 / R=0.844 / F1=0.916**（`--offline` 一条命令口径，240 标签逐位一致）、resume SIGKILL E2E 通过、soak 300s 压测 PASS（无 5xx / 终态率 100% / 结束 total≤50 三项 PASS，RSS 斜率 DEFER=quick 口径诚实标注）。P0-1（成本对数）维持阻塞于 GLM 配额窗口（09-18 单点探针仍 HTTP 429）。

### Added（Wave 26 审计 P0 第四轮清偿：resume 安全一致性 / fallback 收尾 / Go 语言包，docs/25）

- **W26-A resume 安全一致性（P0-11，第六轮审计 F6-R1 清偿）**：server resume 端点补 `_ensure_source_allowed(Path(config.source_path))`——**先校验后 mark_resuming，校验失败不改任务状态**；空 source_path 显式 400（`Path('')` resolve 恒等进程 cwd，交白名单会误导放行）。限流定性：resume 已被全局 POST/DELETE 中间件覆盖（无排除清单），无需改代码，+用例钉住（429 实测断言）。+双发 resume 语义用例（恰一成功一拒绝、run_audit 恰一次）。F5-R3 同卡清偿：understand 对空文本响应降级 debug（离线 FakeLLM 不再打「增强失败」warning；非空内容解析失败仍保留 warning，W15 可见性语义不回退）。+7 用例。
- **W26-B fallback 收尾（P0-7 边界清偿）**：run_audit finally 收口前按 `fallback_used>0` 发 warning 事件（含切换次数与 fallback_model 结构化字段；unwrap 后 getattr 判定，无属性客户端零事件——行为零变化）；`from_sources` env 层接入 `GLM_MODEL_FALLBACK`（CLI/配置文件路径三层合成补全；.env 白名单未扩，显式决策留待后续）。+7 用例。
- **W26-C Go 语言包（SUPPORTED_LANGUAGES 4→5）**：tree-sitter-go 接入——`go_extract.py`（package/const/func 含 receiver `Type.Method`/`(*Type).Method` 限定、import 两形态、调用点；func_literal 体内/泛型实例化/内建函数不做，docstring 明示）、`_AST_LANGUAGES` 扩 go、三规则 `GO-SQL-INJECTION`/`GO-HARDCODED-SECRET`/`GO-LONG-FUNCTION`，**规则库 86→89 条**（docs-site/rules.md 再生）；13 文件语料（4 正例 + 9 反例）E2E 实测 6 命中零误报、AST 13/13、resolved_ratio 0.4 达标。+49 用例。
- 联调与验收：三卡并行 + 统一联调——联调发现并修复 1 处集成冲突（`.go` 后缀 guess_language 既有断言过时 → 更新）；全量 pytest **2239 passed**（基线 2176 + 净新增 63）、ruff 全域绿、demo verified（8.8s）、金标一条命令 **P=1.000 / R=0.844 / F1=0.916**（Go 新规则零金标污染，零回退门连续四轮）、soak 300s 判定见 soak_w10.md。P0-1 维持阻塞（09-19 探针仍 429，连续第四次）。

### Added（Wave 27 审计 P0 第五轮清偿：F7-R1 根治 / 结果版本化 / safe-rename 原语，docs/26）

- **W27-A F7-R1 根治（P0-12，第七轮审计清偿）**：`_load_dotenv` 增加模块级 `_DOTENV_CACHE`——首次成功加载后缓存解析结果，后续调用返回浅拷贝（同进程第二次 `from_env()` 丢 .env 值的实测缺陷 49→0 修复为 49→49；双入口 from_env/from_sources 同治）。「只加载一次」防重复 IO 与「GLM_* 键不写 os.environ」防污染设计均保留；缺文件不置标志可重试语义不变。resume 摘要新增 `LLM 通道：已启用/未启用` 防御性标注；doctor 语言包清单补 go（W26 遗留）。方案 a（cmd_resume 调序）经依赖分析弃做：单 store 循环依赖 + config.work_root 与默认值存在落错库边界，b 落地后前置 from_env 已无害。+4 新用例、5 处 F7 语义演进断言更新（报备）。
- **W27-B 结果版本化（P2 遗留四轮清偿）**：`report_history` 表（PK(audit_id,seq)）+ `set_report` BEGIN IMMEDIATE 单事务「当前 UPDATE + 历史 INSERT」双写（seq 原子分配照 append_event 形态）；`get_report(seq=)` 可选参数（缺省路径零变化）+ `list_reports` 摘要面；delete/prune 级联（同 ID 重建 seq 归 1）；server 新增 `GET /api/audits/{id}/reports` 与 `GET /api/audits/{id}/reports/{seq}`（gray_release 路由冻结断言同步）。+16 用例（含并发 set_report seq 连续 1..24）。
- **W27-C safe-rename 确定性原语 MVP（代际缺口之首首次落地）**：`audit/refactor/rename.py` 纯模块（零 LLM）——plan/apply 两段式：token 级 identifier 节点字节区间替换（字符串/注释物理不可越界）、import 模块路径跳过、非法名/多定义点（宁拒不改）/解析失败计划期即拒；apply 写前「内容一致性 + AST 复检」双校验，任一失败 all-or-nothing。**dogfood 实测 20/20 verified**（跨文件引用全更新、幂等、防 `user`/`username` 误替换）。+16 用例。CLI/server 接线归后续轮。
- 联调与验收：三卡并行 + 统一联调（全量 pytest 期间零并行手动命令，避开 soak 开火窗——W26 教训落实）；**联调发现并修复 1 处集成冲突**（契约用例 `test_config_from_env` 钉住的正是 F7-R1 旧有损语义，方案 b 后失封闭 → 修复为「环境与 .env 双无 → 不可用」的确定性真契约）；全量 pytest **2277 passed**（基线 2239 + 净新增 38）、ruff 全域绿、demo verified（9.0s）、金标一条命令 **P=1.000 / R=0.844 / F1=0.916**（零回退门连续六轮）、rename dogfood 终树复核 20/20。P0-1 维持阻塞（09-19 探针仍 429，连续第六次）。

### Added（Wave 29 审计 P0 第七轮清偿：rename 作用域增强 / soak 排水 / server 语言参数 / 前端报告历史，docs/28）

- **W29-A rename 作用域增强（P0-2，口径「作用域可分则分，分不清则拒」）**：多定义点不再一刀切拒绝——对每个引用点做上下文归属判定：`self.X` 归属所在类同名 method（所在类无定义时沿 bases 一层基类名匹配，继承场景放行）；裸名 X（含 `from m import X` 导入项）归属模块级定义点；`obj.X/cls.X` 等接收者类型不可静态判定的形态记**歧义点**。任一歧义点 → 整体拒绝（维持「宁拒不改」安全边界），errors 含歧义位置明细（file:line + 原因）；全部可归属 → 照常全量替换。单定义点路径行为逐字节零变化。已知边界如实申报：同名参数/局部变量撞名保守误拒、`super().X` 不追、基类只看一层、类型推断不做。+17 用例（dogfood 三场景实测：双类同名 self 调用分别归属、obj.X 拒绝含位置、跨文件继承链放行）。
- **W29-B soak 排水等待（drain grace，第八轮审计起在案的测量口径根治）**：`bench/stress/run_soak_long.py` 新增 `--drain-sec`（env `CODEAUDIT_SOAK_DRAIN_SEC`，CLI 优先，默认 60，0=关闭）——接纳窗口关闭后对在途任务继续轮询至 drain 截止，截止前转终态计 `drain_rescued`（排水收编）、仍超时才计 `drain_still_running`；终态率公式不变、只升不降，FAIL 判定不放松，既有输出行格式/退出码/`--quick` 行为零变化（报告新增「排水归因」一行）。同卡顺带订正 `audit/detect/rules/cpp.py` 一处与实现矛盾的行内注释（W29 审计发现，零行为变化）。+11 用例。
- **W29-C /api/rename 补 language 参数（双入口对齐）**：`RenameRequest.language`（缺省 "python" 零变化）透传 `plan_rename`；非法语言走既有 plan 校验 → 400 中文 detail，端点零新增分支。+3 用例；gray_release 冻结断言零变化。
- **W29-D 前端「报告历史」面板（W27-B 端点消费面最后一公里）**：TaskDetail 新增 ReportHistory 组件——版本列表（seq/时间/健康分/问题数）+ 点击展开懒拉该 seq 版本摘要；空态/404/接口失败统一降级「暂无历史版本」不阻断页面。+6 用例（tsc 零错误、vitest 76 passed、build 绿）。
- **集成收口件**：`codeaudit doctor` 语言包探测补 cpp（清偿 W28 已知边界，照 go 先例，可选级警告）。
- 联调与验收：四卡并行（文件所有权互不相交）+ 统一联调——**零集成冲突**；全量 pytest **2382 passed**（基线 2351 + 净新增 31 逐位对账）、ruff 全域绿、demo verified、金标一条命令 **P=1.000 / R=0.844 / F1=0.916**（零回退门连续九轮确认）、soak 300s+drain 60 新口径首验、前端三件套绿。P0-1 维持阻塞（09-19 探针仍 429，连续第九次）。

### Added（Wave 28 审计 P0 第六轮清偿：safe-rename 接线 / diff 历史消费 / C++ 语言包，docs/27）

- **W28-A safe-rename CLI 接线 + diff 历史消费（能力变现）**：新增 `codeaudit rename <source> <old> <new> [--yes] [--diff-only] [--lang]`——**dry-run 默认**（预览 diff + 「预览模式」标注），`--yes` 才落盘；`--diff-only` 为显式别名且与 `--yes` 互斥退出 1（不静默忽略，防误以为已应用）；计划 errors/apply 失败/路径不存在全部中文报错退出 1 不落盘。`codeaudit diff` 扩展 `--from-seq/--to-seq`——audit_id 按历史版本对比（W27-B 数据面变现），缺省 None 原路径零变化，文件路径+seq 报错，超界中文报错。+12 用例。
- **W28-B rename server 端点（治理链第一版全前置，F6-R1 教训内化）**：`POST /api/rename`（apply 缺省 False）——鉴权随 /api/* 中间件（401 实测）、**SOURCE_ROOTS 白名单第二行即调**（空路径 400 → 白名单 → 存在性，白名单先于存在性不泄露越界路径）、限流随 POST 中间件；plan errors → 400 中文 detail；apply 失败 → **409 Conflict**（资源状态与请求前提冲突语义，all-or-nothing 零落盘）。gray_release 路由冻结断言同步。+9 用例。
- **W28-C C++ 语言包（SUPPORTED_LANGUAGES 5→6）**：tree-sitter-cpp 0.23.4 接入——`cpp_extract.py`（namespace 嵌套限定拼栈、类外定义 `ns::Class::method`、const/constexpr/#define 宏常量口径、调用点三形态；模板实例化/lambda 体/宏展开不做，docstring 明示）+ parsers/store/utils（.cpp/.cc/.hpp）/engine 接线 + 三规则 `CPP-SQL-INJECTION`/`CPP-HARDCODED-SECRET`/`CPP-LONG-FUNCTION`，**规则库 89→92 条**（docs-site/rules.md 再生）；13 文件语料 E2E 实测 6 命中零误报、AST 13/13、resolved_ratio 0.4286。+53 用例。
- 联调与验收：三卡并行 + 统一联调——**一次全绿零集成冲突**；全量 pytest **2351 passed**（基线 2277 + 净新增 74）、ruff 全域绿、demo verified（8.7s）、金标一条命令 **P=1.000 / R=0.844 / F1=0.916**（C++ 新规则零金标污染，零回退门连续八轮）、C++ E2E 终树复核 6 命中逐位一致。P0-1 维持阻塞（09-19 探针仍 429，连续第八次）。

### Added（Wave 22 审计报告短期 P0 清偿，docs/21）

- **W22-D 非回环绑定强制 token（安全默认）**：`codeaudit serve --host <非回环>` 且未设 `CODEAUDIT_API_TOKEN` 且未传 `--allow-insecure` 时拒绝启动（中文报错 + 退出码 1）——堵住"`0.0.0.0` 无鉴权裸奔"部署洞；回环绑定（默认 127.0.0.1/localhost/[::1]）行为零变化。+10 用例（三分支 + 回环判定形态）。
- **W22-C 规则级配置暴露**：`AuditConfig` 新增 `disabled_rules` / `severity_overrides` / `ignore_paths` 三键（默认空 = 行为零变化），配置文件（`.codeaudit.toml` / `[tool.codeaudit]`）与 CLI（`--disable-rule` / `--ignore-path`，可多次）双入口——按项目裁剪审计口径。禁用经既有 `extra["disabled_rules"]` 拦截点（串行/并行同口径，run_rules 与 run_detection 双入口接线）；严重度覆盖在 hits 层单点改写（LLM hints、verify 触发、报告三处同口径）；路径白名单在 build_rule_contexts 过滤（一处同时决定规则扫描与 LLM 审查清单，不影响索引与后处理扫描器）。未知规则 id / 非法严重度记 `extra["rule_config_warnings"]`。+16 用例。
- **W22-A 工具循环消息预算**：`AgentLimits.message_budget_chars`（默认 0=不限）+ `AuditConfig.agent_message_budget`（默认 60_000 字符）——tools 审查路径每轮请求前超预算从最旧 tool 消息折叠 content（保留 160 字符前缀 + 折叠标记，只缩短不删消息保 tool_call/tool 配对，标记幂等，折叠尽仍超则保守停止）。清偿在线实测 completion:prompt=3:1 异常的主因（工具结果逐轮累积不裁剪）。+6 用例。注：`audit/agent/base.py` 为契约文件，本次改动为带默认值增量字段（既有构造点零变化），消费点同批次接线。
- **W22-B 风险聚焦两级审计（默认关闭）**：`llm_review_top_files`（**默认 0=全量，行为零变化**；>0 = LLM 通道只深审风险分 top-N 文件，规则候选仍全量入报告）。风险分 = 规则命中按 SEVERITY_WEIGHT 加权，无命中文件（含 llm_only）按行数降序垫底，tie-break 路径字典序（审计确定性不变量不破坏）；simple/tools/llm_only 全模式生效；生效时 `extra["llm_focus"]` 统计 + 汇总进度事件。默认值翻转判据与 flash 对数结论见 docs/21 §5。+8 用例。
- **W22-F 受影响测试选择**：fix 阶段现有测试验证从整库运行改为优先聚焦——issue 所在符号经调用图 `callers` 方向收集直接调用方中的测试文件（`test_*.py`/`*_test.py`），单文件聚焦运行（沙箱 `run_tests` 原生 target 参数，沙箱层零改动）；聚焦未收集到用例（pytest exit 5）自动整库兜底复跑；收集不到（无索引/无命中/反查异常）回退整库并在事件中如实标注。`fix_stats` 新增 `focused_tests_runs` 计数。+8 用例。
- **W22-E 基准对数（GLM-5.3 Flash）**：消融配置表新增第 8 组 `llm_focus`（top 25）；`bench/w22_focus_compare.py` 一次性对数脚本（full vs llm_focus，10 项目 240 金标，模型 glm-5.3-flash，.env 自动加载口径与 CLI 一致）；dry-run 预检通过（零 Key）。在线真跑因 **GLM 账户余额不足**（HTTP 429）未执行成功、无效产物未落盘——待充值后重跑并回填 docs/21 §5，判据达标才翻 W22-B 默认值。

### W21

- **F5 degraded 误报警告修复**：refactor 阶段事件的 `degraded` 计数字段与编排层降级布尔标志同名，CLI 曾把重构方案 LLM 降级计数误判为 ingest 降级并误打"[警告] ingest 未成功"——判断改为仅认 `degraded is True`，+2 回归用例。
- **自审计门禁 critical 形态误报行内抑制**：taskstore 4+1 处 SQL 构建（f-string 仅拼常量片段/占位符、值全参数化）、config 环境变量名、脱敏测试假密钥夹具共 7 处加 `# codeaudit: ignore[ID]` + 定性理由——`audit-self`（--diff origin/main --fail-on critical）门禁从必红（exit 3）转绿（exit 0，issues 702→696，high/medium/low 零变化）。
- **Web 路由安全标疑规则与三层环语料实测**：新增 `PY-WEB-ROUTE-NO-AUTH`（medium 标疑，FastAPI/Flask 装饰器路由无鉴权特征）与 `PY-WEB-NO-RATE-LIMIT`（low 标疑，文件无限流特征），鉴权/限流豁免口径 + 29 用例；三层间接环（a→b→c→a）语料实测确认为覆盖缺口而非能力缺口（图级识别 + 方案层全链路，+3 用例）。规则库 80 → **82 条**。
- **文档同步**：README / 规则手册 / Roadmap（补 0.5.0/0.6.0 条目）与 CHANGELOG 口径同步。
- 总装终验指标见 `bench/results/w21_总装终验报告.md`。

### Added（Wave 16 专项验收短板清偿）

- **依赖与配置安全扫描（验收 P1/P3 清偿）**：新增 `audit/depcheck/` 全库后处理扫描器——①依赖 CVE 匹配：解析 requirements*.txt / package.json / pyproject.toml [project]，内置 27 条经 GitHub Security Advisory Database 逐条核实的真实 CVE 种子库（pypi 14 / npm 13，含 CVSS→critical/high/medium/low 分级与修复版本建议）；②重复依赖钉扎检出（DEP-DUPLICATE）；③配置文件明文密钥扫描（CFG-SECRET）：.env / .env.* / *.yaml / *.yml / *.properties / *.ini / *.toml 中密钥形态 key，值打码 `********` 呈现，占位值豁免。挂点 `audit.detect.engine._post_scan_issues`（延迟导入、任一扫描器异常只记 `post_scan_errors` 不阻断），`CODEAUDIT_DISABLE_POST_SCAN=1` 全关。专项验收"依赖 CVE 0%、配置文件密钥 0/2"清偿（实测 .env 2/2 + yaml 2/2，critical/high 安全误报 0）。
- **克隆与死代码检测（验收 P1 清偿）**：`audit/detect/crossfile.py`——行归一化（字符串→STR、数字→NUM、去注释）+ K=6 行窗口滚动哈希的全库克隆检测（PY/JS/TS-CLONE，O(n)，15.4 万行最坏情形实测 2.19s，总行数 >30 万护栏跳过）；`audit/detect/deadcode.py`——保守口径死代码（PY 模块级 `_` 私有符号 / JS/TS 非 export 顶层函数全库零引用，豁免 `__init__.py`、dunder、`__all__`、装饰器、tests）。专项验收"克隆 0/2、死代码 0/2"清偿（dup_block 11 行克隆命中、`_unused_var`/`neverCalledHelper` 命中，clean 语料 0 误报）。
- **重构方案分级与工时估算（验收 P2 清偿）**：`RefactorProposal` 新增 `priority`（P0=循环依赖/安全类关联；P1=performance/high 关联或长函数超 200 行；其余 P2）与 `estimated_effort_hours`（循环依赖 8h、热点 行数/100、长函数 超限行数/40、dedup 命中数×1h 启发式）两字段（带默认值，旧报告 from_dict 兼容，schema 不破坏）；只填字段不重排，稳定排序留给展示层。
- **分层架构违规规则（验收 P2 清偿）**：`audit/detect/rules/arch_layers.py`——高层目录（api/controller/handler/routes/presentation/views）直接 import 低层目录（dao/repository/dal/infra/infrastructure/persistence/db）报 PY/JS-LAYER-VIOLATION（style/medium，model/models 有意不入低层表防 schema 误报）。专项验收"跨层调用 0/1"清偿（arch_cycle 的 api→dao 穿透命中）。
- **命名/PII/JS 命令注入规则（验收 P3 清偿）**：`py_naming.py`——PY-NAMING-STYLE（函数 camelCase/类首字母小写）+ PY-PINYIN-NAMING（66 音节表 ≥2 音节组合且非英文技术词，保守启发式）；`pii_rules.py`——PY-PII-LOG（PII 变量名/身份证/手机号进日志，security/medium）；`js/js_security_ext.py`——JS-COMMAND-INJECTION（child_process exec/execSync/spawn 首参拼接，security/high）+ JS-NAMING-STYLE（大写函数名无 `new` 使用）。静态规则库 **63 → 70 条**（Python 39 / JavaScript 24 / TypeScript 专属 7，含 JS/TS 共享 9）。专项验收"JS 命令注入 0/1、PII 0/2、命名规范 0/6"清偿（6 例命名语料 5 例命中——拼音单音节 jia 为有意豁免口径，PII 日志命中、配置密钥 4/4）。

### Added（Wave 16 补充：验收遗留与 P0 真跑）

- **PY-PII-SQL 明文入库规则（验收遗留清偿）**：`pii_rules.py` 新增第二 PII 规则（security/medium）——SQL 写库语句（INSERT/UPDATE/CREATE TABLE）含 PII 列名或绑定参数含 PII 变量即报；口径注明"关注数据最小化而非注入"（参数化绑定的 PII 入库仍报）。专项验收"敏感字段明文入库 0/2"清偿（实测 `INSERT INTO users(phone)` 命中、参数化 SELECT 零误报）。
- **OSV 在线口径（验收遗留清偿，opt-in）**：`audit/depcheck/osv.py`——`CODEAUDIT_OSV_ONLINE=1` 时叠加 api.osv.dev 在线查询（零新增依赖、10s 超时、失败静默降级、单次审计 200 次上限、同（文件，CVE）与种子库去重并标 `source:osv`）；默认关闭零触网。实测 jinja2==3.1.2 在线返回含 CVE-2024-22195 的 10 条漏洞并为 requests==2.30.0 追加 3 条种子库外 CVE。
- **冗余依赖检出（验收遗留清偿）**：DEP-UNUSED（style/low）——requirements 声明但源码零 import（19 条包名↔模块名映射 + PEP503 启发式）、package.json 声明但零 require/import（devDependencies/@types/node 内置白名单豁免）；无对应生态源码的项目跳过（防误报红线）。实测"声明且使用 0 误报 / 未使用命中"。
- **from_sources 加载 .env（README 承诺对齐）**：CLI 路径此前不读 `.env`（仅 server 的 from_env 读），填了 .env 仍判"LLM 未配置"；现 `from_sources` 以 from_env 同款口径自动加载（真实进程环境 > .env > 默认值，GLM_* 不写 os.environ，`_ENV_LOADED` 幂等），无 .env 时行为零变化。
- **P0 在线真跑闭环（验收唯一遗留条件清偿）**：2026-09-15 配置 GLM_API_KEY 后 `bench.real_run` 官方真跑（10 金标项目 / 240 金标）：**Precision(critical+high) 0.884 / Recall 0.900 / F1 0.892**（精确率 ≥85% 目标达成），记录见 [bench/results/run_20260915_online_w16.md](run_20260915_online_w16.md)；`--fix --tests --review-mode tools` 真跑实测 LLM Patch 4 条（可变默认参数→None 哨兵、恒真元组→`a and b` 等，2 条沙箱验证未过诚实降级 needs-review——语料无现成测试时 verified 闸门保守工作）、生成回归单测 3/3 passed。

### Changed（Wave 16）

- `audit/detect/engine.py`：`run_detection` 在非 llm_only 模式下经 `_post_scan_issues` 挂载全库后处理扫描器（克隆/死代码/依赖与配置安全），扫描器模块缺失或异常仅记 `ctx.extra["post_scan_errors"]`，主检测零影响。
- `audit/detect/registry.py` / `rules/__init__.py`：追加注册 W16 规则（63 → 71）。

### Added（Wave 19：深度审计 P1 清偿）

- **圈复杂度数值化（P1）**：PY-CYCLOMATIC-COMPLEXITY（style/medium）——CC=决策点+1（if/elif/for/while/except/and/or/assert，掩码行 token 计数，嵌套函数独立计），阈值 env `CODEAUDIT_CC_THRESHOLD`（默认 10，CC>阈值才报），message 带数值与分支/循环/布尔/异常分布根因。实测 CC=9 不报/CC=14 报（确定性一致），对齐 SonarQube 口径。
- **并发缺陷规则（P1）**：PY-UNSYNCED-SHARED-MUTATION（bug/medium，threading 在场+模块级可变+函数内无锁增强赋值三条件）与 PY-SLEEP-IN-ASYNC（performance/medium，async def 内 time.sleep 阻塞事件循环）。深度审计 6 例语料 4 命中、对照 0 误报。
- **ORM N+1 识别（P1）**：PY-ORM-N-PLUS-ONE（performance/medium）——循环内 `session.query(...).get(/.first(/.all(` 与 Django `objects.get(` 链式；`.execute` 让路既有 IO-IN-LOOP。
- **动态执行四形态（P1）**：PY-DYNAMIC-IMPORT（`__import__`/`importlib.import_module` 非字面量首参）、PY-DYNAMIC-COMPILE（非点号 compile 非字面量首参）、PY-INDIRECT-EXEC（getattr/globals 字符串含危险内建名，security/low 标疑）。深度审计 v_exec 语料 5/5 命中。
- **命令注入三形态增强（P1）**：PY-COMMAND-INJECTION 扩展 `subprocess.Popen(shell=True/首参动态)`、`os.execv*` sh -c、`run(["sh","-c",var])` 列表中转三形态（既有 os.system/os.popen 路径逐字不变，65 条存量命中基线 diff 零差异）。
- **package-lock.json 传递依赖解析（P1）**：depcheck 新增 lock 解析（lockfileVersion 1/2/3，dev/根/超 5000 条护栏，5MB 上限）并入 CVE 匹配；配套修复 **F3**——ingest 此前把 lock 文件列入默认忽略导致工作副本无 lock 可扫，现 `is_ignored_copy` 复制阶段对 ≤5MB lock 清单放行（性能护栏与解析上限同口径）。实测 lodash 4.17.20 CVE-2021-23337 端到端命中。
- **Patch 接口兼容性比对（P1）**：`Patch.compat_notes`（带默认值向后兼容）+ `audit/fix/compat.py`——patch 应用前后公开函数签名 diff（移除/签名变更/方法移除；新增带默认值参数不算破坏），写入 patch 元数据并在事件中提示。
- **Review Prompt v2→v3（SOLID 实测根因修复）**：SOLID 基准（8 违规+3 对照）V2 实测 LLM 语义命中 0/9，根因为审查维度缺失（仅行级引导）；V3 新增单一职责/开闭分支扩展/依赖方向/接口过胖四个设计审查维度（反幻觉约束保持，PROMPT_VERSION v3 + 版本断言同步）。V3 效果对比因 GLM 资源包耗尽（HTTP 429 余额不足）待复测。
- 静态规则库 71 → **78 条**；规则手册同步重生成。

### Added（Wave 20：P2 清偿）

- **Type-2 语义克隆（P2）**：`crossfile.py` 二级通道——Type-1（STR/NUM 归一）未命中的区间做标识符折叠归一（关键字/builtins 保留），≥`CODEAUDIT_T2_MIN_LINES`（默认 8）结构相同片段报 low/0.5 "结构相似（Type-2）"标疑；同函数自相似不报、Type-1 优先、64 组护栏沿用。实测标识符改名镜像函数命中（1/1——第二轮审计的 clone_sem 语料经扩行后含唯一真 Type-2 对），Type-1 15 条零回归、clean 0。
- **默认凭据字典（P2）**：PY-DEFAULT-CREDENTIAL（security/high）——凭据语义命名（password/pwd/secret/token/api_key… 分词边界）+ 32 条弱口令字典（admin/123456/root…整词比对），`${}`/`<...>`/env 豁免。
- **日志伪造（P2）**：PY-LOG-FORGERY（security/low）——日志格式串含 `
` 转义（raw 串豁免）且参数含非常量变量的 log forging 标疑。
- **SQL 常量传播（P2，上轮数据流缺口清偿）**：PY-SQL-INJECTION 二级判定——同文件"SQL 常量变量"（`tmpl = "SELECT..."`）单步传播至 `.format()`/`+ NAME`/f-string 插值使用行；基线 diff 345→347 只增 2 条真阳性零删减。
- **依赖安全升级（P2）**：`pydantic-settings` 钉扎 `>=2.14.2`（堵 CVE-2026-58203）、`pygments>=2.20`（堵 PYSEC-2026-2987）、dev 组升级 `pytest>=9.0.3` + `pytest-asyncio>=1.4`（堵 PYSEC-2026-1845；pytest 9 零适配成本，asyncio_mode=auto 全兼容）；pip-audit 复查项目直接依赖零漏洞条目，server 冒烟通过。
- 静态规则库 78 → **80 条**。

### Fixed（Wave 18：第二轮深度审计发现）

- **配置文件自动发现回退被审项目根（F2，README 承诺对齐）**：`.codeaudit.toml` 自动发现此前仅查 CWD——从其他目录（及 serve 常驻场景）审计项目时项目根配置静默失效。现保持 CWD 行为零变化，仅当 CWD 无 `.codeaudit.toml` 且被审路径为目录时回退项目根发现（`.codeaudit.toml` / `pyproject.toml [tool.codeaudit]`）；+2 回归用例。附带 W18 深度审计实测：安全变种泛化 8/8 命中且误报 0、反序列化变种 3/3、死代码动态引用防御全对、自定义规则注册 API 可用；量化缺口（并发/DB 0%、Type-2 克隆 0/2、复杂度无数值、lock 传递依赖漏报）登记于 [bench/results/audit_round2_20260915_深度审计.md](audit_round2_20260915_深度审计.md) 待后续 Wave 清偿。

### Fixed（Wave 17：赛题合规严审计发现）

- **规模超限降级运行的机器可读标志与门禁联动（F1，FR-1.4）**：项目超过 2000 文件 / 50 万行上限时 ingest 降级运行——此前仅 stderr 警告 + `files_total=0` 间接信号且退出码 0，CI 调用方解析 stdout JSON 无法区分"空项目"与"审计未实际执行"。现 `AuditStats` 新增 `degraded_ingest: bool = False`（ingest 失败时置 True，向后兼容），`--check/--fail-on` 门禁模式下降级运行直接判未通过（退出码 3）；普通模式保持退出码 0 + stderr 警告，不阻断交互式使用。附带严审计实测记录：JS/TS 修复闭环在线真跑 verified（SQL 拼接→参数化，node --test 2/2 全绿）、前端契约 12 端点与 server 零漂移、对抗八场景全 OK、GBK 编码容错、报告三格式 10/10 内容项。

### Added（Wave 15 审计清偿）

- **服务端安全治理（审计 P0）**：①API 鉴权——`CODEAUDIT_API_TOKEN` 设置后 `/api/*`（除 health）要求 `Authorization: Bearer <token>` 或 `X-API-Token`，失败 401（常量时间比较），未设置时启动打 WARNING 提醒仅限本机；②源路径白名单——`CODEAUDIT_SOURCE_ROOTS` 非空时 `POST /api/audits` 的 source_path 限白名单根内（越界 400 且先于存在性检查，防路径存在性泄露）；③写端点限流——`CODEAUDIT_RATE_LIMIT`（次/分钟，按 IP 滑动窗口，超限 429，默认关）；④SSE 并发上限 50（超限 429，连接结束归还名额）。方案见 docs/20 §4.1。
- **LLM 审查路径密钥打码（审计 P0，补 W14 缺口）**：LLM 审查产出的 `code_snippet` 逐行套用字面量打码、`evidence` 逐条过 `mask_secret_text`（audit/detect/base.py 新增）——此前 LLM 路径的密钥行会明文进入报告与下载接口；回归测试覆盖 FakeLLM 含密钥 payload 全链路。
- **TaskStore 多 worker 一致性（审计 A3 三连清偿）**：①`busy_timeout=5000` + 写操作 locked/busy 指数退避重试（≤3 次）；②`append_event` 改 `BEGIN IMMEDIATE` 单事务原子分配 seq（`INSERT...SELECT COALESCE(MAX(seq),0)+1`）；③`sweep_interrupted(grace_seconds=)` 宽限清扫——行表新增 `updated_at`（自动迁移回填，幂等），多 worker 部署设 `CODEAUDIT_SWEEP_GRACE_SEC` 规避兄弟 worker 运行中任务被误清扫的状态复活竞态；server lifespan 接线并在关停时 close store。附 2 连接 × 8 线程 × 400 事件并发压测（零 OperationalError、seq 严格单调无断档）。
- **重构执行闭环（审计 A8）**：`--fix` + LLM 可用时，refactor 阶段取 confidence 最高 ≤3 条启发式 proposal 合成 `[Refactor]` 前缀 Issue 复用既有 fix 管线生成可 apply 补丁（结构校验→git apply→tree-sitter+ast 双保险→沙箱测试→四态 FixStatus/失败回滚），产物自然进入 report.patches——重构能力自"建议生成器"补齐"可执行"半环；离线/未开 --fix 零行为变化。`syntax_ok` 对 Python 补 `ast.parse` 双保险（tree-sitter 容错解析对语义级语法错误有漏报面）。
- **CI 自举与依赖安全（审计 A4/A5）**：①依赖下限钉扎堵已知 CVE（fastapi≥0.115、jinja2≥3.1.6、python-multipart≥0.0.18）；②ci.yml 覆盖率门禁 `--cov-fail-under=85`（2026-09-15 实测 87% 棘轮基线，只升不降）+ pip-audit 步骤 + 删除过授的 issues:write；③新增 `pr-audit.yml`——PR 触发离线自审计（`--diff origin/main --fail-on critical`，SARIF artifact），审计工具自此审计自己；Makefile 增 `audit-self` 本地一键同款。
- **测试盲区补齐（审计 A9）**：新增 utils（74 例）/pipeline 契约（12）/prompts（14）/architecture 降级路径（7）专属测试 + 前端 SevTag/StatusTag 组件测试（10）；卡F 同步产出 architecture.py 静默吞噬 6 处证据清单（本波已修复，见 Fixed）。

### Changed（Wave 15）

- **PY/JS 规则族收敛（审计 A6）**：新建 `audit/detect/rules/_scan_common.py`（call_span/find_call/enclosing_function/indent_width/is_blank 等逐字重复件上收）与 `_rule_families.py`（TodoFixme/LongFunction/DeepNesting/MagicNumber 四族参数化基类），python.py/javascript.py/js_ext.py 改薄封装，净减约 170 行重复；63 条规则 id/severity/文案/命中行为零变化（16 项 PY/JS 奇偶校验 + 既有金标命中门禁守护）。`audit/errors.py` 删除零引用的 `IndexBuildError`/`ReportError`。
- **魔法值常量化**：patcher 的 git 超时 30s/截断 500、fix stage 截断 300、Docker 内存 512m/CPU 1 提为具名常量（值不变）。
- `fix/stage` 修复候选扩展：critical/high 之外放行 `[Refactor]` 前缀 medium（闭环需要，普通 medium 仍不修复）。

### Fixed（Wave 15 集成修复）

- **architecture.py 静默吞噬记账（审计 A7 最重簇）**：6 处 except 静默降级补 logging（信息收集类 debug、LLM 增强失败 warning）——此前 LLM 故障对外完全不可见；降级行为本身零变化（卡F 测试继续全绿）。
- **W14 沙箱压测用例偶发 flaky 修复**：`test_grandchild_pipe_holder_does_not_hang_run` 原在 t=0 仅打印一次，负载下击杀前排空可能未读到该块（实测约 1/6 失败率）；改为在超时窗口内持续输出，回归意图不变。

### Added（Wave 13）

- **CI 在线评估工作流**（`.github/workflows/online-eval.yml`）：workflow_dispatch 手动 + schedule 每周一 UTC 0:00（repo 变量 `ONLINE_EVAL_SCHEDULED` 控制，默认 false 防误烧 token）；`secrets.GLM_API_KEY` 存在性守卫（缺失自动跳过并标注）；`bench/eval/run_online_eval.py` 新增 `--ci` 模式（简洁输出、`EVAL_RESULT=PASS|FAIL` 语义化退出码，评估不过 workflow 红）；concurrency 防并发、FAIL 也上传 artifact。
- **Docker 沙箱后端**（`audit/sandbox/`）：`docker run --rm --network none --memory 512m --cpus 1 -v <cwd>:/work -w /work`；启动前探测（`which` + `docker info` 探针，进程内缓存），探测失败自动降级子进程路径并在 `SandboxResult.backend` 诚实标注；镜像默认 `python:3.12-slim`（`CODEAUDIT_DOCKER_IMAGE` 可覆盖）。**默认 opt-in 关闭**（CI 实证自动启用会把宿主解释器包进容器导致全 127，裁决见 e2685b0）。
- **多 worker 压测基线**（`bench/stress/run_soak_multiworker.py` + [bench/results/soak_multiworker_w13.md](bench/results/soak_multiworker_w13.md)）：双 worker 混合负载 / RSS / 三态判定与单 worker 对照表，额外观测 429 全局准入触发率、active 计数一致性、跨 worker SSE。
- **前端降级展示**（`frontend/`）：任务详情页预算熔断警示条（含 used_tokens/token_budget）、token 消耗统计、任务列表降级标记（由 report 字段前端推导，不改 server 契约）；vitest 组件用例。

### Fixed（Wave 14 审查清偿）

- **密钥打码（NFR-11 清偿）**：检测到的硬编码密钥在报告 `code_snippet` 中以 `********` 打码呈现（PY/JS 两条密钥规则命中行做值脱敏，变量名与引号结构保留、不泄露长度）——此前明文密钥原样进入 JSON/MD/HTML 报告，与「数据隐私」声明矛盾（实测复现后修复，附管线级回归测试）。
- **Docker 沙箱接线与容器清理**：沙箱后端配置全层贯通（`codeaudit run --sandbox-backend` / `CODEAUDIT_SANDBOX_BACKEND` / 配置文件 > 默认 `subprocess`，Docker 后端自此可从配置启用）；docker 路径超时与收尾补 `docker rm -f` 容器清理（`--name codeaudit-<uuid>`）——此前仅 kill CLI 进程，容器继续运行、`--rm` 失效。
- **沙箱管道挂死清偿**：子进程击杀后先关管道促读端 abort + `proc.wait()` 短兜底 + 排空协程 `asyncio.wait` 兜底超时——Windows 下孙进程继承管道写端曾可致 `run()` 永久挂起、服务任务卡死 running（本机实测挂死 ~21s → ~1.5s 返回，截断如实标注）。
- **服务端运维收口**：①任务工作副本磁盘回收——prune / 启动 sweep / DELETE 三个删行时机同步回收 `<work_root>/<audit_id>/` 与 uploads zip（仅终态任务；路径校验防注入；清理失败仅告警不阻断删行）；②`CODEAUDIT_MAX_RUNNING/PENDING` 改惰性解析，`.env` 配置自此真实生效（与 `CODEAUDIT_DB_PATH` 口径对齐）；③上传路径准入二次校验闭合 TOCTOU（读流后建任务前权威复查，拒绝时临时文件零残留，429 响应与快速路径同形）。
- **`max_tool_iterations` 接线（NFR-10 口径对齐）**：review 工具迭代上限改由配置消费（默认 12，与原硬编码一致；配置文件可调），此前为无消费点的死配置。
- **zip 防御加固**：解压全程复核实际写出量（单文件实际 > 声明、累计 > 上限即中止，不再只信中央目录声明值）；Windows 保留设备名（CON/NUL/COM1-9 等，含带扩展名形态）成员过滤。
- **文档一致性收口**：README 徽章 / Roadmap / 文档索引 / 目录结构 / 沙箱描述五处过期修正；frontend 版本对齐 0.6.0；本文件 Wave 12 段误入的 4 条 Wave 11 重复条目移除、比较链接更新。

### Changed（Wave 14）

- `SandboxExecutor` 新增 `backend` 权威入口（`use_docker` 参数保持向后兼容，非法值诚实降级并在进度事件标注）；`SandboxResult` 注释更新（极端兜底场景 `exit_code` 可为 None，`timed_out=True` 已诚实标注）。

## [0.6.0] - 2026-09-15

Wave 9 + Wave 10 + Wave 11 + Wave 12：测试体系补全（W9）、服务治理与灰度基建（W10）、形态演进（W11）、在线 GLM 安全治理与评估体系（W12）。契约 v2.1/v2.2 微增 + F6–F9 清偿，全部向后兼容；W9–W12 方案分别见 docs/14–docs/17。

### Added（Wave 12）

- **在线 GLM 接入与评估体系**：真实 GLM Key 接入（`.env`，已 gitignore）——在线审计实测**双通道融合生效且 LLM 独立发现规则漏报缺陷**（mini_app：`textutil.py` slugify 空分隔符死循环 high）；**提示注入鲁棒性实测通过**（伪 SYSTEM/IGNORE ALL payload 零生效零回显，真缺陷全保留）；成本基准建立（mini_app 约 150s / 7–9 调用 / 3–5 万 tokens）。评估套件 `bench/eval/run_online_eval.py`（注入鲁棒性 / 离线 vs 在线增值 diff / 成本基准三场景；**显式 `--online` 才真实调 GLM**，双守卫防误烧 token；quick 实测 7/7 判据 PASS）。
- **内存归因结论（F9 清偿）**：`bench/memdiag/` 逐任务 tracemalloc + 600s 长窗 soak + 服务端 10 项逐证——**结论：有界增长非泄漏**（一次性预热 ~10MB + 稳态残差 ≤13KB/任务收敛于平台期，窗口加倍斜率减半；FIFO 50 兜底下上界 ≈85-90MB），零必须修复项。
- **预算熔断在线语义硬化（F8 清偿）**：编排层全局预算闸门 `_BudgetGateLLM`（覆盖 simple 审查/verify/understand/refactor 增强/fix/testgen 全部 LLM 调用；此前仅 tools 路径受约束且按文件重置可烧 10 倍预算）——熔断发 warning、后续 fix/testgen 显式跳过、done 事件诚实标注 `degraded+budget_tripped`+最终累计账目；真实小预算（3000）实测熔断中途触发、调用冻结、报告正常产出（推理模型单笔 thinking 可超剩余预算属闸门"发起前设卡"语义的既定边界，诚实记录）。
- 文档站 nav 补登记 Wave 8–12 方案文档（mkdocs strict 构建通过）。

### Changed（Wave 12）

- **F6 清偿（Key 脱敏）**：任务持久化的 `config_json` 落库前 `api_key` 替换为 `"<redacted>"`——运行时 Key 一律来自进程环境（`from_env`），store 任何路径取不到明文 Key（sqlite3 裸连全表扫 + 主库/WAL/SHM 字节级扫描用例固定）。
- **F7 清偿（.env 自动加载）**：`AuditConfig.from_env` 自动发现并加载 CWD/`.env`（零依赖解析：注释/export 前缀/引号/坏行容错；同进程至多加载一次；**真实环境变量逐键优先**）；GLM_* 三键只合并进本次取值不写进程环境，CODEAUDIT_* 经 setdefault 传递（server 感知）；收口修复 `_get_store` 的加载时序（from_env 先行，否则 .env 的 CODEAUDIT_DB_PATH 静默失效）。

### Added（Wave 11）

- **任务持久化（契约 v2.2）**：新模块 `audit/taskstore.py`——SQLite WAL 任务存储（标准库零依赖，线程锁串行化写），任务、事件流、报告全部落库（`<work_root>/audits.db`，`CODEAUDIT_DB_PATH` 可覆盖）；**服务重启后终态任务与报告仍可查询下载**（新能力），遗留非终态启动时 sweep 为 failed（`服务重启中断`）；FIFO 容量淘汰迁移到 store。启动 sweep 经 FastAPI lifespan 执行（导入零副作用）。
- **线程池执行**：审计任务从共享事件循环迁到独立线程（`asyncio.to_thread`，每任务独立事件循环）——根治 CPU 密集段饿死服务循环的问题（W9 观察到的 POST 响应推迟、health 失联不复现），读端点在重审计负载下全程可响应。
- **协作式取消（语义诚实声明）**：DELETE 不再瞬时打断线程，改为事件边界取消——执行协程每次 emit 前检查取消标志/表项存在性（表项被删即取消），七阶段均频繁 emit，典型亚秒级生效；DELETE 的 HTTP 语义不变（204 + 全端点 404 + SSE 收流）。
- **多 worker（实验特性）**：`codeaudit serve --workers N`（默认 1）——N>1 以 import string 形式启动 uvicorn 多进程，sticky 执行模型（任务由接收 worker 执行），SQLite 跨 worker 可见性经双 worker 冒烟实证（跨进程建/查/删/报告全通）；429 准入计数升级为全局口径（count_active 走 store），并发上限 = workers × 每 worker 上限。
- **内存归因诊断工具**（`bench/memdiag/run_memdiag.py`）：逐任务 tracemalloc 快照 diff + RSS 采样——**结论：库层（纯规则）RSS 稳态斜率 ≈ 0**，soak 观察的每任务缓爬不在 audit/* 复现（归因服务端簿记层，W11 持久化改造后由 soak 复跑复核）；报告见 [bench/results/memdiag_w11.md](bench/results/memdiag_w11.md)。

### Changed（Wave 11）

- SSE 事件流改 store seq 游标读取（回放/跟随语义不变）；既有 server/integration 用例随持久化迁移适配（断言语义未放宽，适配清单见 docs/16 收口记录）；`bench/stress` 场景 5 清理逻辑随形态更新（db 随场景临时目录销毁）。

### Added（Wave 10）

- **任务准入控制（F5 清偿，契约 v2.1）**：全局信号量限制同时运行的任务数（`CODEAUDIT_MAX_RUNNING`，默认 4），超出的任务停留在既有 queued 状态排队执行；非终态任务总数达上限（`CODEAUDIT_MAX_PENDING`，默认 20）时 `POST /api/audits` 与 `/api/audits/upload` 返回 **429**（`服务器并发审计数已达上限（N），请稍后重试`）——契约 v2 其余响应码不变。排队中任务 DELETE 语义不变（取消 + 「任务已取消」）。
- **金丝雀双版本回放工具**（`bench/canary/replay.py`，`make canary`）：git worktree 检出旧 tag 起双端口服务，固定请求集（health / 列表 / mini_app 上传审计至 done / 报告三格式 / issues）双发对拍，归一化（键级剥离 audit_id/created_at/duration_sec/schema_version + 值级替换任务 ID）后 diff——diff 非空即语义漂移，阻断发布（退出码 1）。自验：v0.5.0 vs HEAD 7/7 项一致 PASS。
- **Soak 持续混合负载压测**（`bench/stress/run_soak.py`，`make soak`）：2 Hz 读负载 + 8s 周期双素材任务提交 + 30s SSE 拍点 + 10s RSS/health 采样；判定四准则（无 5xx / 接纳任务终态率 100% / RSS 稳态斜率 < 1 MB/min / 结束 total ≤ 50）；报告内置 RSS 诊断节（全窗口 vs 稳态窗口斜率、RSS-任务数相关性）。
- **沙箱输出限量 + truncated 标记（F4 清偿）**：`_drain` 改为 8MB 限量排空（超限后继续读但丢弃，杜绝子进程 PIPE 写阻塞误杀），`SandboxResult` 新增 `truncated` 字段，被限量的 tail 首行标注 `[output truncated]`；`run()/run_tests()` 签名与既有语义不变。
- 文档站 nav 补登记 Wave 8/9/10 三份方案文档（mkdocs strict 构建通过）。

### Changed（Wave 10）

- **流式上传（F2 清偿）**：`POST /api/audits/upload` 改为 256KB 分块落盘（`.part` 临时文件 + `os.replace` 原子提交），累计超 200MB 立即 413 并停止读取——不再全量缓冲进内存（W9 对抗基线 210MB 上传 RSS 峰值 660MB，复跑验证回落，见 bench/results/adversarial_w10.md）；400/413/取消路径临时文件零残留，落盘字节逐一致语义不变。
- adversarial runner 适配契约 v2.1：A3 场景 F5 段升级为准入实证（25 路并发 > pending 上限，校验无 5xx / active 峰值 ≤ 20 / 被接纳者全终态）、A6 场景新增 RSS 回落校验（>400MB 判失败）、A7 场景适配 truncated 标记语义；风险登记表 F2/F4/F5 状态更新为已修复。
- soak RSS 判定口径修订（W10-A5 集成裁决）：剔除前 60s 预热样本（冷启动懒加载导入属一次性抬升非泄漏），稳态样本不足回落全窗口；全窗口斜率与 RSS-任务数相关性继续如实报告，每任务 ~0.15MB 缓爬的长窗口归因列 W11 待办。

### Added（Wave 9）

- **对抗与滥用测试运行器**（`bench/adversarial/run_adversarial.py`，`make adversarial`）：八场景真实 HTTP + 库层双口径——恶意 zip 军火库（路径逃逸 / 1GB 声明量炸弹 / 坏 zip / 空 zip / 奇葩文件名 / 40 层深嵌套 / 二进制与 3MB 单行 .py，含 marker 全树逃逸扫描）、参数与路径滥用矩阵（20 探针精确 4xx）、任务表洪泛（60 任务 FIFO 淘汰 + 并发准入实证）、create→DELETE 抖动竞态、100 路 SSE 中途 DELETE 收流、210MB 不可压缩上传（413 + 服务端 RSS 取证）、沙箱逃逸四件套（超时击杀 / 200MB 输出炸弹 / 白名单拒绝 / 越出 cwd 写文件取证）、提示注入离线实证（注入 payload 不影响规则通道对真缺陷的判定）。报告见 [bench/results/adversarial_w9.md](bench/results/adversarial_w9.md) 与复跑 [adversarial_w10.md](bench/results/adversarial_w10.md)。
- **算法不变量测试**（`tests/property/test_algorithm_invariants.py`，进 CI）：审计确定性（同输入两次问题清单逐字段一致）、健康分单调性（追加 critical 不升分，50 组随机性质 + 值域边界）、干净语料零 critical/high 误报、SARIF 2.1.0 结构不变量、summary/issues 计数自洽、过滤与分页端到端不变量（severity 归一化语义、offset 游走拼回无重无漏）。
- **灰度发布保障测试**（`tests/integration/test_gray_release.py`，进 CI）：旧版报告（v0.3 形态，缺 v1.7 字段）在当前代码可反序列化 + md/html/SARIF 三格式渲染 + 往返不漂移；特性开关 A/B 等价性（rule_scan_workers 串行 vs 并行、ingest 硬链接 vs 复制，语义快照相等）；API 路由面冻结（openapi paths 与契约 v2 清单精确相等，多删都红）。
- **审查发现登记 F1–F5**（docs/14 §1）：无鉴权任意路径审计（取证：仓库外目录可审计且报告回传源码片段）、上传全量缓冲、Windows 沙箱无文件系统隔离、沙箱输出无界缓冲、任务无准入控制（F2/F4/F5 已于 W10 清偿，F1/F3 属本地工具设计边界继续登记）。

## [0.5.0] - 2026-09-13

Wave 8：Web 前后端。配套前端从单文件演示页升级为工程化 React 工作台（`frontend/`，React 18 + TypeScript + Vite + Ant Design 5 + ECharts），REST API 扩展为契约 v2（任务列表 / 删除 / zip 上传 / 重构方案 / 架构理解 / 健康检查），`python cli.py serve` 单进程同时服务 API 与前端构建产物。技术选型经 GitHub 开源调研后锁定（antd 99.5k★ / ECharts 67.3k★ / DefectDojo 交互范式参考），方案与任务分解见 [docs/13](docs/13-Wave8总体方案-Web前后端.md)。

### Added

- **前端工作台 `frontend/`**（W8-A2 / W8-A3）：三页式 SPA——「仪表盘」（任务列表：状态标签、创建时间、查看 / 删除，非终态任务 5s 自动刷新）、「新建审计」（服务端本地路径 / zip 拖拽上传双入口 + 修复与单测开关）、「任务详情」（运行中：七阶段 Steps 进度 + SSE 实时事件日志（断流自动降级轮询）；完成后：健康分 gauge 与严重度分布（ECharts）、问题列表（severity / category 服务端过滤 + 关键词过滤 + 展开详情与代码片段）、修复补丁 diff 视图、重构方案卡片、架构理解通用渲染、报告三格式下载）。中文界面，antd zhCN，类型化 API 客户端与后端契约逐字对齐。
- **REST API 契约 v2**（W8-A1）：`GET /api/health`、`GET /api/audits`（分页任务列表，新→旧）、`DELETE /api/audits/{id}`（终态移除 / 运行中先取消）、`POST /api/audits/upload`（multipart zip，魔数与 200MB 上限校验，413 / 400 明确报错）、`GET /api/audits/{id}/refactors`（重构方案，暴露 0.4.0 已有但未上 API 的 RefactorProposal）、`GET /api/audits/{id}/understand`（架构卡片）。SPA 静态托管：`frontend/dist` 存在时根路径与客户端路由兜底到 SPA，否则回退单文件演示页（保留为零构建入口）；开发态 CORS 精确放行 Vite 5173 端口。
- **Web 压测基线**（`bench/stress/run_stress_web.py`）：真实 HTTP 口径四场景——读端点突发（32 并发 × 560 请求）、并发审计（6 任务同时提交到终态）、SSE 并发流（24 连接依赖历史回放）、zip 上传（12 次 + 非 zip 负样本必须 400）；数据见 [bench/results/stress_web_w8.md](bench/results/stress_web_w8.md)。
- **CI 前端 job**：typecheck（tsc --noEmit）+ vitest + vite build 三关，与 Python 三矩阵并行。

### Changed

- 任务表条目新增 `created_at / source_path / do_fix / do_tests`（列表与详情端点可见）；依赖新增 `python-multipart`（multipart 上传运行时必需）。
- 任务列表 / 删除为**内存态**（进程重启即清空），如实标注，持久化留作后续版本。

## [0.4.0] - 2026-09-12

Wave 7：赛题合规收口与算法提速。补齐赛题要求的最后一块功能拼图（自动生成重构方案），打通 JS/TS 修复与单测验证闭环，并行/硬链接提速以可选开关落地并附诚实压测对比。合规矩阵 10 项对照的最终状态见 [docs/12 §7](docs/12-Wave7总体方案-赛题合规与提速.md)。

### Added

- **重构方案生成器**（`audit/refactor`，W7-A2）：确定性启发式 + LLM 增强双层——确定性层零 LLM 可出（长函数 top-N 分解、重复代码聚类、热点模块拆分、循环依赖提示，全部从已有索引与规则命中聚合），LLM 层深化每条方案的 rationale/steps（未配置 Key 时安全降级为纯启发式）；审计报告新增**「重构方案」章节**（md / html / json 三格式同步，SARIF 不含重构项），报告契约 v1.7 微增 `RefactorProposal`（id/title/target/kind/rationale/steps/related_issues/source）。
- **JS/TS 修复与单测验证闭环**（`audit/fix` / `audit/testgen`，W7-A3）：JavaScript / TypeScript 问题同样进入修复闭环——语法验证走 tree-sitter 重解析 + `node --check`（探测可用才启用），单测验证走 `node --test`（node 18+ 内置，探测可用才执行，不可用时语法验证结果诚实标注），沙箱白名单相应扩展；CI 的 ubuntu / windows runner 自带 node 实证。
- **压测对比数据**：W7 vs W6 同负载对拍（串行 vs 并行规则扫描、硬链接 vs 复制物化、命中集合一致性校验），数据见 [bench/results/stress_w7_clean.md](bench/results/stress_w7_clean.md)。

### Changed

- **性能实验与开关化**（W7-A1）：规则扫描多进程并行（`rule_scan_workers`，`0` 自动 / `1` 串行默认 / `>1` 指定并发，文件数 ≥100 才启用进程池）与 ingest 同盘硬链接物化（`link_same_volume`，**默认关闭**走整树复制，跨盘 / 失败自动回退）均作为**可选配置**交付。本机 2000 文件档实测并行仅 -7.1%、硬链接路径相对复制 +70.3%，负收益已诚实回退默认值（规则并行默认串行、ingest 默认复制），设计保留待后续复跑；行为等价有保障——并行与串行的规则命中集合指纹一致（41 = 41）。

## [0.3.0] - 2026-09-12

Wave 5 + Wave 6：质量攻坚与健壮性清偿、规则库扩充、依赖约束治理。Wave 5 三路只读审查 + Dogfood 自审计共产出 68 项发现，本版修复其中 25 项核心问题，并新增联调测试套件（34 用例）与压力测试基线（2000 文件档纯规则审计 0.245 s/KLOC）；Wave 6 完成 R4 复核清偿（含 4 项必修缺陷）、静态规则库 49 → 63 条并上线内置规则手册、依赖约束治理。

### Added

- **规则库扩充至 63 条**：静态规则库 49 → 63 条（Python 35 / JavaScript 21 / TypeScript 专属 7），新增 14 条规则（Python 8、JavaScript 与 TypeScript 6），每条均附正反例测试；新增**内置规则手册** [docs-site/rules.md](docs-site/rules.md)——由 `python scripts/gen_rule_docs.py` 从规则注册表自动生成，含全部规则的判定说明与统计，规则与文档不再漂移。

### Fixed

- **密钥规则复数形态漏报（上版回归）**：0.2.1 引入的标识符分词精确匹配未覆盖复数形态，`API_KEYS` / `dbPasswords` / `credentials` 等命名全部漏报；token 归一化补齐复数形式后重新命中，并附复数形态正反例回归测试。
- **增量索引陈旧缓存**：索引预取缓存在同进程多次构建间未重置，改动 import 后的增量构建仍按陈旧依赖解析符号；构建开始时统一清空缓存，并补「改 imports 二次构建」回归用例。
- **删除 / 重命名补丁回滚**：补丁备份与生效性校验此前只覆盖 `+++ b/` 侧路径，删除型与重命名型补丁回滚不完整；现按 diff 两侧路径并集处理，覆盖全部补丁形态。
- **LLM 审查生效修复（critical）**：simple 审查模式下回调参数错绑，导致 LLM 通道完全不参与审查、报告却按「规则 + LLM」口径呈现；修复后 simple 模式真实生效。
- **修复 Patch 静默跳过**：嵌套 git 仓库场景下 `git apply` 可能在用户原始项目的外层仓库执行而被静默跳过；补丁的应用与回滚现严格限定在审计工作副本内。
- **SARIF 文件 uri 跨平台**：Windows 上生成的 SARIF `uri` 含反斜杠、不符合规范；统一归一为 `/` 分隔，各平台均可通过 upload-sarif 校验。
- **密钥规则误报**：PY-HARDCODED-SECRET 规则改为标识符分词精确匹配敏感词，并叠加熵 / 字符集随机性校验，普通命名常量（如内部指纹键名）不再被判为 critical。
- **服务端报告目录按任务隔离**：报告改写入 `<work-root>/<audit_id>/reports/`，并发或连续多次审计不再相互覆盖。

### Changed

- **understand 阶段随 ingest 门控**：ingest 失败时理解阶段与报告统计一并跳过，不再回读用户原始目录。
- **任务取消终态落盘**：客户端断开或任务取消（CancelledError）时任务以终态落盘并退出运行队列，不再悬挂为 running。
- **依赖约束治理**：完成第三方依赖与 CI Action 版本约束的收敛治理，降低供应链与构建漂移风险。
- **接入失败门控**：ingest 失败时 index / detect / fix / testgen 全部跳过并发「跳过：…（ingest 未成功）」事件，报告为空壳——绝不触碰用户原始目录、绝不向原项目写入补丁。
- **资源收口**：审计结束统一关闭 LLM 客户端与索引连接，长跑与批量场景不再泄漏连接。
- **服务端任务表有界化**：终态（已完成 / 失败）任务记录按 LRU 淘汰，服务长期运行内存不再无限增长。
- **门禁消息走 stderr**：`[门禁] 通过 / 未通过` 判定信息输出到 stderr，`--json --check` 组合下 stdout 为纯 JSON，可整体 `json.loads`。
- **`.codeaudit/` 默认忽略**：审计工作区目录加入流水线默认忽略清单（与 `.git`、`node_modules` 并列），对已含工作区的项目复跑不再产生嵌套副本与假问题，无需手动配置 gitignore。
- **程序名动态显示**：帮助 / usage 中的程序名按调用方式显示——`python cli.py` 显示 `cli.py`，安装态显示 `codeaudit`。
- **文档站与协作设施完善**：文档站导航收录设计文档 00、首页链接口径对齐；CLI 文档补全 `--fail-on` 单独指定即隐含启用门禁、程序名与门禁消息流向说明；README 区分离线基线（`bench.run`，零 Key 可跑）与真跑评估（`bench.real_run`，需 API Key）、注明 `.env` 不自动加载需手动 export、目录树补全 `scripts/` 与压测目录、Makefile 清单补 `clean`；离线 run 记录头部加注消融表配置数口径；CI / 压测调试残留路径统一纳入 `.gitignore` 与 ruff 排除。

### Security

- **超长行搜索降级**：除 zip 接入防护外，沙箱内的文件搜索对超长行自动降级处理，恶意构造的超长行不再拖垮审查进程。
- **zip 炸弹防护**：zip 接入增加 1 GB 累计解压上限，超限中止并明确报错，恶意超大压缩包不再拖垮进程。
- **测试目标注入防护**：run_tests 工具拒绝以 `-` 开头或越界的测试目标，阻断参数注入路径。

## [0.2.0] - 2026-09-11

Wave 4：开源生态对标（SARIF / CI 门禁 / 增量审计 / 配置文件 / 打包入口 / 协作与文档设施）。

### Added

- **SARIF 输出**：`--format sarif` 生成 SARIF 2.1.0 报告 `report.sarif`，配合 `github/codeql-action/upload-sarif@v3` 可上传 GitHub Security tab（公共仓库免费）；报告契约新增 `schema_version` 字段。
- **CI 门禁**：`--check --fail-on <sev>`（`critical|high|medium|low`），命中阈值时退出码 `3`；退出码约定固化为 `0` 完成 / `1` 运行错误 / `2` bench 缺 key / `3` 门禁失败。
- **增量审计**：`--diff <git-ref>` 只审相对 ref 发生变更的文件，面向 PR 场景。
- **基线抑制**：`--report-baseline` 生成存量问题基线（问题指纹 sha1），`--baseline` 加载基线抑制命中项并计入 `stats.suppressed`；行内忽略 `# codeaudit: ignore[规则ID]`。
- **配置文件**：支持 `.codeaudit.toml` 与 `pyproject.toml [tool.codeaudit]` 自动发现，优先级 CLI > 环境变量 > 配置文件 > 默认值，未知键 / 非法值输出警告（`config_warnings`）。
- **入口点**：`pip install -e .` 后提供 `codeaudit` 命令，版本号单源（`pyproject.toml` 与 `audit.__version__` 一致）。
- **GitHub 协作设施**：issue / PR 模板、CODEOWNERS、Dependabot（pip 与 github-actions 每周巡检）、`release.yml`（tag 触发自动建 Release）与 `docs.yml`（文档站自动发布 gh-pages）。
- **文档站**：mkdocs-material 站点（`docs-site/`），含 CLI 全参数速查、SARIF 上传实践、PR 增量审计实践与 Roadmap；设计文档 01~09 由构建期镜像收录。

### Changed

- README 收口：新增「数据隐私」「CI 门禁与增量审计」「配置文件」「Roadmap」节，徽章区补 Release / Docs，安装节补 `codeaudit` 入口用法。

## [0.1.0] - 2026-09-11

Wave 1~3：核心流水线、检测与修复能力、产品化入口与评估基建。

### Added

- **七阶段流水线**：Ingest（路径 / zip、语言识别）→ Index（tree-sitter 符号表 + 调用图，SQLite）→ Understand（map-reduce 架构卡片）→ Detect（双通道）→ Fix（Patch 生成 + 沙箱验证）→ TestGen（pytest 生成 + 沙箱运行 + 失败重试）→ Report。
- **双通道检测**：tree-sitter AST + 正则静态规则库 49 条（覆盖 bug / performance / security / style，支持 Python / JavaScript / TypeScript）负责高召回；LLM Review Agent（上下文注入、`find_references` 等工具取证）+ Verify Agent 复核负责高精确；未配置 `GLM_API_KEY` 自动降级纯规则离线模式。
- **修复与单测闭环**：critical / high 生成 unified diff，经 `git apply --check`、语法重解析、运行项目现有测试三重验证，通过才标记 `verified`；对目标函数生成回归单测并在沙箱运行，失败带 traceback 重试。
- **报告三格式**：JSON / Markdown / HTML + 健康分（加权问题密度）；单阶段失败只记入报告不中断审计。
- **三端入口**：CLI（`run / index / report / serve`）、REST API（异步任务 + SSE 进度 + 仪表盘）、单文件 Web 演示页。
- **评估基准**：240 条金标（10 个项目集）、匹配与消融脚本、效率对比；离线纯规则基线实测 Precision(critical+high) 1.000 / Recall 0.844 / P50 5.0 s/KLOC（见 `bench/results/`；LLM 通道指标待真跑）。
- **工程化**：650 项单元测试全绿（Wave 3 收口基线）、GitHub Actions CI（Python 3.11 / 3.13 × ubuntu / windows 矩阵）、离线全闭环演示 `python demo/run_demo.py`。

[Unreleased]: https://github.com/mingkiiiiing/codeaudit-agent/compare/v0.6.0...HEAD
[0.4.0]: https://github.com/mingkiiiiing/codeaudit-agent/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/mingkiiiiing/codeaudit-agent/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/mingkiiiiing/codeaudit-agent/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/mingkiiiiing/codeaudit-agent/releases/tag/v0.1.0
