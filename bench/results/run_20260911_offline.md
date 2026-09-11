## Run 2026-09-11 ｜ model=offline-fakellm（纯规则基线） ｜ prompt_ver=n/a ｜ 配置=full / −verify / rules_only × 10 项目（api_key='' 强制离线，level=critical+high）

| 指标 | 值 | 样本量 |
|---|---|---|
| Precision (critical+high) | 1.000 | 报告 74 条 / 金标 90 条 |
| Recall (critical+high) | 0.844 | 金标 90 条 |
| F1 (critical+high) | 0.916 | — |
| 耗时 P50 / P90 | 5.0 / 7.3 s/KLOC | 10 项目 |
| tokens/KLOC | 0k prompt + 0.0k completion | 10 项目 |

## 消融对比

| 配置 | Precision | Recall | F1 | 耗时 P50 | 耗时 P90 | tokens/KLOC | 备注 |
|---|---|---|---|---|---|---|---|
| full | 1.000 | 0.844 | 0.916 | 5.022 | 7.288 | 0k + 0.0k | |
| −verify | 1.000 | 0.844 | 0.916 | 4.827 | 7.361 | 0k + 0.0k | |
| rules_only | 1.000 | 0.844 | 0.916 | 4.617 | 6.910 | 0k + 0.0k | |

## 备注

- **本记录为离线纯规则基线，LLM 相关指标（精确率 85% 目标、tokens/KLOC）待提供 GLM_API_KEY 后真跑。**
- 离线环境（api_key 强制为空 → FakeLLM）下 full / −verify / rules_only 三配置退化为同一条纯规则路径，三行指标差异仅为计时噪声；「−verify 使 Precision 下降」等消融结论必须在真跑环境复测后才可引用。
- 耗时为纯规则流水线（ingest→index→understand→detect→report）的本机量级：P50≈5.0 / P90≈7.3 s/KLOC，不含 LLM 调用与沙箱验证耗时；docs/04 的 30 s/KLOC 目标须以真跑口径为准。
- tokens/KLOC = 0：离线无任何 LLM 调用、无 token 消耗、无缓存行为。
- Recall(critical+high) 未达 1.0 的缺口共 14 条，其中 14 条为 git-history 来源（缺陷已在合成项目修复提交中消除，HEAD 审计天然不可检出，应在其父提交快照上对账，见「未命中金标」节）；其余 0 条为 injected/manual 来源的真实漏报。
- 数据集：bench/datasets/goldset.jsonl 共 240 条金标（critical+high 90 条），项目集 10 个（合成 4 + 注入变体 4 + demo_proj 及其注入变体），源码合计约 2943 行。
- 复现：python bench/datasets/gen_offline.py --with-run（清空重建数据集后重跑，注入 seed 固定，金标可复现）。

## 项目明细

| 项目 | LOC | 耗时(s) | s/KLOC | Issue 数 |
|---|---|---|---|---|
| shopcore | 276 | 1.42 | 5.14 | 16 |
| blogengine | 215 | 0.78 | 3.62 | 13 |
| datatools | 286 | 0.98 | 3.44 | 22 |
| webapi | 239 | 1.04 | 4.37 | 13 |
| shopcore_inj | 407 | 2.33 | 5.73 | 40 |
| blogengine_inj | 336 | 1.13 | 3.35 | 34 |
| datatools_inj | 396 | 1.94 | 4.91 | 43 |
| webapi_inj | 369 | 2.27 | 6.14 | 37 |
| demo_proj | 142 | 1.03 | 7.26 | 14 |
| demo_proj_inj | 277 | 2.08 | 7.52 | 38 |

## 未命中金标（critical+high，full 配置）

说明：下列未命中金标**全部来自 git-history 来源**——对应缺陷已在合成项目的修复提交中消除，HEAD 版本上不再存在，因此在 HEAD 审计中天然不可检出（docs/04 §2.1 口径：这类金标应在其父提交快照上对账，tests 的对齐抽样即按父提交版本核对，实测 100% 可命中）；若未命中清单中出现 injected/manual 来源的 critical+high 金标，才属真实漏报。

- [git-history] fix: avoid shared mutable default in discount coupons :: pricing.py L11-L11
- [git-history] fix: avoid shared mutable default in discount coupons :: pricing.py L9-L9
- [git-history] fix: compare format extension with equality instead of identity :: aggregates.py L26-L26
- [git-history] fix: deploy hook without shell string concat (command injection) :: hooks.py L11-L11
- [git-history] fix: drop unreachable raise after return in scheduler retry :: schedule.py L20-L21
- [git-history] fix: drop unreachable raise after return in scheduler retry :: schedule.py L22-L22
- [git-history] fix: invoke tar with argument list to avoid command injection :: server.py L11-L11
- [git-history] fix: narrow exception handling in pagination parsing :: handlers.py L40-L40
- [git-history] fix: remove eval usage in directive rendering (security hardening) :: markdown.py L31-L31
- [git-history] fix: replace dead raise after return with warning log in report exporter :: reports.py L40-L41
- [git-history] fix: replace dead raise after return with warning log in report exporter :: reports.py L42-L42
- [git-history] fix: stop sharing mutable registry default across cache rebuilds :: cache.py L6-L6
- [git-history] fix: stop sharing mutable registry default across cache rebuilds :: cache.py L8-L8
- [git-history] fix: use parameterized query to close sql injection in customer lookup :: orders.py L36-L36

## 离线声明

- **本记录为离线纯规则基线（offline rules-only baseline）**：运行时强制 `api_key=""`，audit 流水线走 FakeLLM 纯规则路径，全程零网络、零 LLM 调用、零 token 消耗。
- 离线环境下 `full` / `−verify` / `rules_only` 三配置退化为同一条纯规则路径（`llm_available=False` 时 review/verify 通道不生效），三行指标差异仅为计时噪声；「−verify 使 Precision 下降」等消融结论必须在 GLM_API_KEY 真跑环境复测后才可引用。
- **本记录为离线纯规则基线，LLM 相关指标（精确率 85% 目标、tokens/KLOC）待提供 GLM_API_KEY 后真跑**；在拿到真跑 run 记录之前，本文件中的 Precision/Recall 不得对外引用为系统真实水平。
- 纯规则基线耗时量级（个位数 s/KLOC、本机 CPU、小规模合成项目）不能外推到真跑口径（含 LLM 调用与沙箱验证）；docs/04 的 30 s/KLOC 目标以真跑记录为准。
- 复现方式：`python bench/datasets/gen_offline.py --with-run`（清空重建数据集后重跑三配置，注入 seed 固定）。
