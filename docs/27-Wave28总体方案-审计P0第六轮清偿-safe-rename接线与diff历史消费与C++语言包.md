# Wave 28 总体方案：审计 P0 第六轮清偿（safe-rename 接线 / diff 历史消费 / C++ 语言包）

> 依据：第八轮审计报告 `bench/results/audit_round8_20260919_复审.md`（73/100 B）P0 清单。
> 工作轮路由：第八轮审计（提示词一）完成 → 同对话工作轮（提示词二）。
> 执行方式：三卡并行后台代理 + 集成人统一联调（与 W23~W27 同款流程）。

## 1. 范围裁决

| 审计 P0 项 | 本轮处置 | 理由 |
|---|---|---|
| 存量-A safe-rename CLI/server 接线（能力变现） | ✅ 卡 A + 卡 B | MVP dogfood 20/20 已就绪，接线即用户可达 |
| 存量-B diff 接历史 seq（版本化消费面） | ✅ 卡 A | W27-B 数据面就绪只差消费面 |
| C++ 语言包（多语言 5/7→6/7） | ✅ 卡 C | Go 卡模式第三次复制，风险低收益确定 |
| P0-1 成本对数 | ⏸ 跳过 | 09-19 探针仍 429（连续第八次），等配额窗口 |
| soak 首次 FAIL 定性 | 巡检自动化承接 | 36/36 收口后按自附判据出结论，本轮不设卡 |

## 2. 卡片交付（文件所有权互不相交）

- **卡 A = rename CLI + diff seq**：`cli.py` 新增 `codeaudit rename <source> <old> <new> [--yes] [--diff-only] [--lang]`——**dry-run 默认**（预览 diff + 「预览模式（未落盘，加 --yes 应用）」标注），`--yes` 才落盘；`--diff-only` 为显式别名且与 `--yes` 互斥（不静默忽略，防误以为已应用）；计划 errors/apply 失败/路径不存在全部中文报错退出 1 不落盘。`codeaudit diff` 扩展 `--from-seq/--to-seq`（缺省 None=原路径零变化；seq 走 TaskStore.get_report(audit_id, seq)；文件路径+seq 报错；超界中文报错）。+12 用例（7 rename + 5 diff seq）。
- **卡 B = rename server 端点**：`POST /api/rename`（source_path/old_name/new_name/apply，apply 缺省 False 对应 --yes 语义）——**治理链第一版全前置**（F6-R1 教训内化）：鉴权随 /api/* 中间件（401 实测）、SOURCE_ROOTS 白名单 `_ensure_source_allowed` 第二行即调（空路径 400 → 白名单 → 存在性 400，白名单先于存在性不泄露越界路径）、限流随 POST 中间件；plan errors → 400 中文 detail；apply 失败 → **409 Conflict**（资源状态与请求前提冲突的准确语义，非 500）。+9 用例；gray_release 冻结断言同步。
- **卡 C = C++ 语言包**：`audit/indexer/cpp_extract.py`（namespace 嵌套限定拼栈、类外定义 `ns::Class::method`、const/constexpr/#define 宏常量口径、#include、调用点三形态；边界明示：模板实例化/lambda 体/宏展开/条件编译不做）+ parsers/store/utils（.cpp/.cc/.hpp）/engine 接线 + 三规则（CPP-SQL-INJECTION/SECRET/LONG-FUNCTION），**规则库 89→92 条**；13 文件语料（4 正例 + 9 反例）。+53 用例（30 indexer + 23 规则）。tree-sitter-cpp 0.23.4 钉扎。已知边界：doctor 不探测 cpp（cli.py 禁改，照 go 先例）、.h 不入扩展名映射（C/C++ 歧义）。

## 3. 集成人联调记录

- 改动面核对：三卡文件与申报逐项吻合，零越权。
- **统一联调一次全绿，零集成冲突**（W26/W27 各有 1 处，本轮为 0）：全量 pytest **2351 passed, 2 skipped**（341.57s）＝基线 2277 + 净新增 74（卡A 12 / 卡B 9 / 卡C 53）逐位对账；ruff 全域绿；demo **verified 8.7s**；rules.md 再生 **92 条**；金标一条命令 **P=1.000 / R=0.8444**（`w28_goldset_20260919.md`）——C++ 三条新规则零金标污染，**零回退门连续八轮**；C++ E2E 终树复核 **6 命中逐位一致**（SECRET3/SQL2/LONG1）+ 反例零误报 + AST 13/13 + resolved_ratio 0.4286。
- P0-1：09-19 探针仍 429（连续第八次），w22_focus_compare 对数继续阻塞，如实记档。
- 联调纪律执行：pytest 期间零并行手动命令；03:24 挂套件（soak 04:07 开火前 34 分钟完成），窗口安全。

## 4. 已知边界（如实申报）

1. 卡 A：`cmd_diff` 缺省路径 audit_id 定不到时仍走 locate_audit 的 FileNotFoundError 顶层兜底（保持缺省行为逐字节零变化）；历史定位遇 CODEAUDIT_DB_PATH 指向不存在的库时报「任务库不存在」不顺手建空库。
2. 卡 B：并发修改 409 用例的注入点为 monkeypatch plan_rename 薄壳（apply_rename 保持原实现）；dogfood 语料 CRLF 检出问题已以二进制对账规避。
3. 卡 C：doctor 不探测 cpp；.h 头文件不入扩展名映射（C/C++ 歧义）；跨行函数签名与 operator 重载不做长函数作用域；C++ 原始串 R"(...)" 扫描器不做专门处理。

## 5. 下一轮入口

- **下轮路由=待用户新开 GLM-5.3 对话贴提示词一（第九轮审计）**，复审重点：①三卡新增面（rename CLI 的 dry-run 默认与互斥语义、server 端点治理链完整性、cpp_extract 口径与 store 分派）；②P0-1 第九次探针；③soak 36/36 收口结论与 FAIL 定性；④W14~W28 积压催办（提交+push+打版 0.7.0+License）。
- 工作轮候选：①P0-1（等窗口）；②rename 作用域增强（同名不同作用域区分）；③前端 /reports 展示接线；④extract-function 原语。
