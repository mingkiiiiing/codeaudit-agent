# 11 Wave 6 总体方案：依赖治理、健壮性清偿与 0.3.0 发布

版本：v1.0 ｜ 日期：2026-09-12 ｜ 前置：docs/10（Wave 5 已交付，824 测试 + CI/Docs 全绿 @ e1c3d77）

---

## 1. 审查结论（本轮起点）

- **R4 修复复核**（W5 的 25 项修复逐项复核）：大部分复核通过；**4 项需本轮修**——
  - R4-1（high）：R1-9 SECRET 规则引入复数漏报回归（`API_KEYS`/`dbPasswords`/`credentials` 全部 MISS，旧版全命中）
  - R4-2（medium）：R1-8 预取缓存跨 build 不重置 → 增量 build 按陈旧 imports 解析（潜伏正确性缺陷）
  - R4-3（medium）：R1-5 备份/生效性校验只覆盖 `+++ b/` 侧 → 删除型/重命名补丁回滚不完整
  - R4-4（medium）：understand 与报告统计未随 ingest_ok 门控（读侧扫用户原目录）
  - 低危择机：R4-5 close 双关抛错、R4-6 切片路径静默失败、R4-7 弱口令漏报、R4-8 ReDoS 分块、R4-9 FIFO 口径 + CancelledError 终态、R4-10 degraded 标志
- **Dogfood 复扫**（W5 修复后）：critical 3→2、high 5→4（SECRET 误报修复生效）；剩余为 store.py 内部常量 SQL（无注入面，用行内抑制处理）与插入循环 executemany 化
- **Dependabot**：6 个升级 PR 全部 open，其中 4 个 CI 红（mkdocs-material 9.7 / tree-sitter <0.27 / mkdocs-gen-files 0.6 / checkout 7）

## 2. 目标与验收

| 目标 | 验收 |
|---|---|
| G-A W5 修复回归清偿 | R4-1~R4-4 修复 + 指定低危项；全量测试零回归；金标对齐不降 |
| G-B 依赖治理收口 | 6 个 PR 逐个处置（合并/关闭附理由）；main 的 CI/Docs 保持绿；tree-sitter 约束决定有据 |
| G-C 规则手册与扩充 | docs-site 规则参考页自动生成（构建期）；Python +8 / JS+TS +6 条新规则各带正反测试 |
| G-D 发布 0.3.0 | 版本单源 bump、CHANGELOG、tag v0.3.0 触发 GitHub Release（release.yml） |
| 底线 | 联调 + 压测复跑无退化；推送后 CI/Docs 全绿 |

## 3. 任务分解表（4 个大任务，两批并行）

| 代号 | 任务名 | 独占目录/文件 |
|---|---|---|
| W6-A1 | 依赖治理 | `pyproject.toml`（依赖约束行）、`.github/workflows/*.yml`（action 版本）、`requirements-docs.txt`；通过 GitHub API 处置 PR（凭据由集成人注入说明） |
| W6-A2 | 健壮性清偿 | `audit/**` 既有文件内修复、`server/app.py`、`web/index.html`、`audit/detect/rules/python.py`（R4-1 + 行内 ignore 示范） |
| W6-A3 | 规则手册与扩充 | `scripts/gen_rule_docs.py`、`docs-site/rules.md`（生成产物）、`audit/detect/rules/python_ext.py`、`audit/detect/rules/js/js_ext.py`、`audit/detect/registry.py`（追加注册）、对应新测试 |
| W6-A4 | 发布 0.3.0 | `audit/__init__.py`（版本）、`CHANGELOG.md`、`README.md`（数字/特性）、`docs-site/roadmap.md`、`docs-site/index.md`、`mkdocs.yml`（nav 补规则手册与 docs/10/11） |

**冲突隔离**：`audit/detect/rules/python.py` 归 A2（只修不扩）；A3 的新规则一律放 `*_ext.py` 新文件；`mkdocs.yml` 归 A4；`pyproject.toml` 归 A1（依赖行）。

## 4. 修复清单（W6-A2）

必修：R4-1（token 归一化含复数形态，补复数正/反例）、R4-2（build 开头 clear 两个新缓存 + "改 imports 二次 build"回归测试）、R4-3（_diff_targets 解析 `--- a/` 侧并集，删除/重命名补丁备份与生效性校验覆盖）、R4-4（understand 与报告统计随 ingest_ok 门控）。
择机：R4-5（close 幂等）、R4-6（切片路径 review_errors）、R4-7（password 家族放低闸门 div≥2/H≥3.0）、R4-9（finally 落终态 + 注释口径 FIFO）、R4-10（done 事件 degraded 标志 + CLI stderr 警告）。
Dogfood 收尾：store.py 两处内部常量 SQL 加 `# codeaudit: ignore[PY-SQL-INJECTION]` 行内注释（吃自己的狗粮，注释注明"标识符为内部常量白名单"）；插入循环改 executemany（store.py:158/165/182/218 处）。
明确不修（继续记录）：R1-19 事件结构化 status 改由 W6-A2 的 R4-10 一并轻量处理（degraded 标志）；R1-24/R1-28 维持 roadmap。

## 5. 协作规约（继承 docs/06~10）

1. A2 每条修复附回归测试；SECRET 规则改动后 `tests/unit/detect` + 金标对齐必须绿，并补复数形态正反例。
2. A3 的规则扩充每条 ≥1 正 ≥1 反测试；新规则 id 前缀沿用 PY-/JS-/TS-；文档生成器幂等。
3. A1 对 GitHub PR 的写操作（评论/关闭/合并）仅使用集成人提供的本机 GCM 凭据通道，且每一步先报告后执行；不确定的 PR（如 tree-sitter 大版本）宁可不合并。
4. 底线：全量 pytest 零回归（基线 824）、ruff 零告警、demo exit 0、tests/integration 全绿、压测 500 档复跑无退化。

## 6. 发布流程（集成人执行，A4 备料）

CHANGELOG/版本/README 就绪 → 全量验收 → 合流推送 → CI 绿 → 打 tag `v0.3.0` 推送 → release.yml 自动建 Release → 确认 Release 页与附件说明。
