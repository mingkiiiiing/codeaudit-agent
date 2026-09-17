# 23-Wave24 总体方案：Java 语言包 / resume 断点续跑 / 体验快赢

> 2026-09-17 ｜ 状态：已完成（联调实测数据见 §4） ｜ 前置：第四轮审计（`bench/results/audit_round4_20260917_复审.md`，70/100，P0 四项新清单）
>
> 工作流：审计轮（提示词一）→ 工作轮（提示词二）循环；本轮为工作轮，三卡并行（文件所有权互不相交，`cli.py` 独占归卡 B）+ 集成人统一联调收口。

## 0. 背景与选卡依据

第四轮审计 P0 清单 → 本轮三卡：

| 审计条目 | 一句话 | 本轮处置 |
|---|---|---|
| P0-4 | Java 语言包（3/7 → 4/7 语言；AstParseGate 基建已就绪，"可立即开工"） | **卡 A（本轮）** |
| P0-6 | resume 断点续跑 + 报告对比（中断=全损；连续两轮顺延） | **卡 C（本轮）** |
| P1-快赢 | verify_stats·ast_wiring 渲染 / CLI 实时进度 / doctor·init（三轮遗留的半天级项） | **卡 B（本轮，固定"每轮至少一张小卡"惯例）** |
| P0-1 / P0-7 | 成本翻默认 / 模型路由 | 维持外部阻塞（GLM 429 余额），充值后按 docs/21 §5 对数 |
| 第四轮 P2 | apply symlink / snapshot 内存 / 多补丁隐式约定 | 联调阶段集成人视余力清偿 |

## 1. 任务卡与所有权（只准改本卡列出文件；`audit/models.py` 等契约文件禁改）

### W24-A：Java 语言包（P0-4）

- **文件所有权**：`pyproject.toml`（仅依赖段追加 `tree-sitter-java`）、`audit/indexer/parsers.py`、`audit/indexer/store.py`（语言白名单与 java 符号提取分支）、`audit/utils.py`（语言识别）、`audit/detect/rules/java.py`（新）、`audit/detect/rules/__init__.py`（注册）、新测试文件（`tests/unit/indexer/test_java_*.py`、`tests/unit/detect/test_rules_java.py`、语料 `tests/corpus/java/` 新目录）
- **交付物**：
  1. `tree-sitter-java` 依赖接入：parsers 注册 java 语言（`get_parser("java")`），`SUPPORTED_LANGUAGES` 扩为 4 语言；`store.py` 语言白名单放行 java。
  2. java 符号/imports/调用点提取（参照现有 python/js 提取实现同库同表）：类/方法/字段符号、import 声明、方法调用点 → `symbols/imports/calls/call_edges`。
  3. content-based 语言识别双档：扩展名优先，`.java` 无歧义可维持扩展名档；`guess_language` 对未知扩展名回退内容探测（`package ` / `public class` 特征）。
  4. 三规则移植（形态对齐 python 版同款规则，AST 佐证走 AstParseGate 现有通路）：JAVA-SQL-INJECTION（字符串拼接 SQL 进 execute）、JAVA-HARDCODED-SECRET（熵闸门 + 字符集口径复用 `_scan_common`/python 同款）、JAVA-LONG-FUNCTION（>80 行，复用复杂度口径）。
  5. 语料：`tests/corpus/java/` ≥10 文件（含注入/密钥/长函数正例与干净反例），定向测试断言：索引 resolved_ratio ≥ 0.4、三规则命中正确、python/js 金标既有命中零变化。
- **验收**：`python -m pytest tests/unit/indexer tests/unit/detect -q` 绿；`ruff check audit cli.py server` 绿；规则注册计数断言同步（现有 test_rules_ext 的计数口径 + JAVA 三条）。
- **禁改**：金标 jsonl 存量文件、`audit/models.py`、`cli.py`、`audit/report/**`、`audit/taskstore.py`。

### W24-B：体验三件（P1-快赢）

- **文件所有权**：`cli.py`（独占）、`audit/report/builder.py`、`audit/report/render.py`、`audit/report/templates/report.md.j2`、`audit/report/templates/report.html.j2`、`tests/unit/cli/*`、`tests/unit/report/*` 既有文件追加
- **交付物**：
  1. `verify_stats` 与 `ast_wiring` 进报告：md/html 摘要节渲染（verify 四态 checked/confirmed/rejected/uncertain + AST parsed/degraded 生效率）；无数据时节省略（老报告/离线模式兼容）。JSON 通道随 `ctx.extra` 已有，不另加字段。
  2. CLI 实时进度：`cmd_run` 消费既有事件流（事件已收集，只差打印），逐阶段输出 `[stage n/7] 描述 (current/total)`，`--quiet` 可关；不破坏 `--check` 门禁 stdout 纯净契约（进度走 stderr）。
  3. `codeaudit doctor`：零 Key 可跑——Python 版本/git/沙箱后端可用性/tree-sitter 语言包（含 java）/`.env` 白名单键/配置文件合法性，输出中文诊断表，异常项给修复建议；`codeaudit init`：写 `.codeaudit.toml` 注释模板骨架（已存在则拒绝覆盖）。
- **验收**：定向 pytest 绿（新增 doctor/init/进度用例 + report 渲染用例）；`--check` stdout 纯净契约既有测试不回退；`ruff` 绿。
- **禁改**：`audit/detect/**`、`audit/fix/**`、`audit/taskstore.py`、`audit/orchestrator/**`、`audit/models.py`。

### W24-C：resume 断点续跑 + 报告对比（P0-6）

- **文件所有权**：`audit/taskstore.py`、`audit/orchestrator/pipeline.py`、`audit/report/comparator.py`（新）、新测试文件（`tests/unit/taskstore/*` 追加、`tests/unit/report/test_comparator.py`、`tests/integration/test_resume.py`）
- **交付物**：
  1. resume MVP：stage 粒度 `stage_done` 清单持久化（每阶段完成即记，尽量由既有事件流推导，避免新表则允许 taskstore 加列）；进程重启恢复同任务时跳过已完成阶段；重启 sweep 不再把带 `stage_done` 的 running 任务粗暴置 failed，改置 interrupted 可续跑。
  2. 索引阶段复用判定：同 source 指纹且索引库存在 → 跳过重建（项目级缓存的最小版，只做 resume 场景，不做通用缓存目录）。
  3. 报告对比纯模块 `audit/report/comparator.py`：`compare(report_a, report_b) -> {fixed, new, persisted}`，issues 按 `(file, rule, ±3行)` 匹配；CLI 子命令 `codeaudit diff <id1> <id2>` 的**接线不做**（`cli.py` 归卡 B），模块提供 `__main__` 入口可独立运行，CLI 接线由集成人联调时统一加（≤10 行）。
- **验收**：模拟中断（pytest monkeypatch 抛异常）→ 恢复 → 已完成阶段不重跑（事件流断言）；demo 项目两次审计 compare 输出与手工比对一致；`ruff` 绿。
- **禁改**：`audit/models.py`、`cli.py`、`audit/report/builder.py`/`render.py`/`templates/**`、`audit/detect/**`、`audit/indexer/**`。

## 2. 设计决策记录

1. **cli.py 独占给卡 B**：三卡 CLI 面需求都存在，但并行改同一文件必冲突；C 的 diff 接线延后到集成人（≤10 行），换取真并行。
2. **resume 不做通用缓存目录**：第四轮审计的增量索引方案（项目级缓存库）是中期工程；本轮只做 resume 场景的索引复用判定，控制爆炸半径。
3. **金标双门**（第四轮风险 4 落地）：存量 10 项目逐位一致（零回退门）与 Java 语料增量达标（新增门）分开断言，Java 接入不碰存量金标文件。
4. **verify_stats/ast_wiring 渲染走既有 extra 通道**：不加 AuditReport 字段（models.py 契约不动），模板按"有则渲染无则省略"。

## 3. 已知边界（诚实声明）

- Java 为首轮接入：符号提取覆盖类/方法/字段/调用点，泛型/注解处理/lambda 内调用不在本轮口径（语料与测试按此口径写）。
- resume 覆盖"进程被杀后重启续跑"；单阶段中途断点（fix 第 N 个 patch）不在本轮粒度。
- P0-1/P0-7 维持外部阻塞，本轮不含。
- apply 三件 P2（symlink/snapshot/断言）视联调余力，不承诺。

## 4. 验收（集成人统一联调实测，2026-09-17）

- **逐卡定向**：W24 三卡新增测试合计 **105 用例**（A：java 提取 26 + 规则 18；B：doctor 16 + init 3 + 进度 6 + 渲染 8；C：taskstore 10 + comparator 15 + resume 集成 3）全部通过；`ruff check audit cli.py server` 全域绿。卡 A 两处清单外最小增量已申报并经集成人复审接受（`registry.py` 注册 2 行——不注册则规则不可用；`test_rules_ext.py` 计数断言同步——任务要点明确要求）。
- **集成人接线四件**：①engine `_AST_LANGUAGES` java 进 AST 名单（python/java 双语言分派，js/ts 维持行级）；②`codeaudit diff <A> <B>` CLI 子命令（audit_id / report.json 路径双支持）；③`scripts/gen_rule_docs.py` java 语言表 → docs-site/rules.md 重新生成（86 条规则入库）；④apply P2 清偿（symlink 目标规划期+写入期双拒绝、生成侧顺序语义显式化）。
- **联调发现并修复 5 处集成冲突**（同一根因）：W24-C 新增的 `stage_done` 事件缺 `message` 键，撞上 4 个基线测试的事件遍历断言（`test_baseline_diff` ×4 + `test_budget` ×1 的 `e["message"]` KeyError）——修复选**给 stage_done 事件补 message**（事件协议不变式「每个事件都有 message」，基线测试零改动；CLI 进度按 type 过滤不受影响）。
- **统一联调**：全量 pytest **2140 passed**（2036 基线 + 净新增 104，5 分 06 秒）、ruff 全域绿、demo 离线闭环 verified（10.0s）、金标 10 项目 240 标签 **P=1.000 / R=0.844 / F1=0.916** 与基线逐位一致（`w24_goldset_20260917.md`）、Java 语料端到端实测：三规则命中 8 处（密钥 ×4 / SQL 注入 ×3 / 长函数 ×1）且 8 个反例文件零误报、AST 解析 13 文件生效率 100%、`cli.py doctor`/`init`/进度 stderr 实测符合预期。
- 阻塞项维持：P0-1/P0-7 与 W22-B 默认值翻转均卡 GLM 账户余额（HTTP 429），充值后按 docs/21 §5 判据复跑。

## 5. 下一轮入口

- 按审计↔工作循环：本轮工作收口后，下一轮 = 新开 GLM-5.3 对话贴提示词一（第五轮审计），复审重点：W24 三卡质量（java 符号提取与三规则口径、resume 状态机与 interrupted 语义、doctor/init/进度/检测质量渲染）+ apply P2 清偿复核 + P0-1/4/6/7 状态复核。
- 工作轮候选（按优先级）：①P0-1/P0-7（外部阻塞解除后第一时间）；②resume 扩展：issues 落盘使 understand 之后阶段可续跑 + `codeaudit resume` CLI 接线（本轮 API 已就绪未接 CLI，见 §3 边界）；③Go 语言包视 Java 验收（本轮 resolved_ratio 0.517 ≥ 0.4 达标，已具备跟随条件）；④架构债：detect↔agents 循环引用收敛、分层白名单配置化、扫描器注册表化。
