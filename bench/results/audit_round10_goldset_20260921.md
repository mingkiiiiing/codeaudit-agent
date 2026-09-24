# Bench 运行结果

- 生成时间：2026-09-21T08:33:47+00:00
- 严重度层级：critical+high
- 项目数：10

## 检测质量

| 指标 | 值 |
|---|---|
| level | critical+high |
| precision | 1 |
| recall | 0.8444 |
| f1 | 0.9157 |
| counts.reports_total | 333 |
| counts.reports_in_level | 74 |
| counts.reports_matched | 74 |
| counts.goldens_total | 240 |
| counts.goldens_in_level | 90 |
| counts.goldens_matched | 76 |

## 耗时（s/KLOC）

| 指标 | 值 |
|---|---|
| n | 10 |
| sec_per_kloc_p50 | 2.7147 |
| sec_per_kloc_p90 | 3.852 |
| sec_per_kloc_mean | 3.408 |

### 分项目明细

| 项目 | LOC | 耗时(s) | s/KLOC | Issue 数 |
|---|---|---|---|---|
| demo_proj | 142 | 1.70 | 12.00 | 15 |
| blogengine | 215 | 0.59 | 2.74 | 13 |
| blogengine_inj | 336 | 0.63 | 1.88 | 42 |
| datatools | 286 | 0.55 | 1.94 | 23 |
| datatools_inj | 396 | 0.69 | 1.73 | 57 |
| demo_proj_inj | 277 | 0.82 | 2.95 | 52 |
| shopcore | 276 | 0.74 | 2.69 | 16 |
| shopcore_inj | 407 | 1.14 | 2.81 | 50 |
| webapi | 239 | 0.60 | 2.49 | 13 |
| webapi_inj | 369 | 1.05 | 2.84 | 52 |

## 成本

| 指标 | 值 |
|---|---|
| kloc | 2.943 |
| prompt_tokens | 0 |
| completion_tokens | 0 |
| prompt_tokens_per_kloc | 0 |
| completion_tokens_per_kloc | 0 |
| cache_hits | 0 |
| cache_misses | 0 |
| cache_hit_ratio | N/A |

## 修复

| 指标 | 值 |
|---|---|
| total | 0 |
| syntax_ok | 0 |
| syntax_ok_ratio | N/A |
| entered_verify | 0 |
| verified | 0 |
| verified_ratio | N/A |

## 未命中金标

- [git-history] fix: stop sharing mutable registry default across cache rebuilds :: cache.py L6-L6
- [git-history] fix: stop sharing mutable registry default across cache rebuilds :: cache.py L8-L8
- [git-history] fix: invoke tar with argument list to avoid command injection :: server.py L11-L11
- [git-history] fix: remove eval usage in directive rendering (security hardening) :: markdown.py L31-L31
- [git-history] fix: compare format extension with equality instead of identity :: aggregates.py L26-L26
- [git-history] fix: drop unreachable raise after return in scheduler retry :: schedule.py L20-L21
- [git-history] fix: drop unreachable raise after return in scheduler retry :: schedule.py L22-L22
- [git-history] fix: avoid shared mutable default in discount coupons :: pricing.py L9-L9
- [git-history] fix: avoid shared mutable default in discount coupons :: pricing.py L11-L11
- [git-history] fix: replace dead raise after return with warning log in report exporter :: reports.py L40-L41
- [git-history] fix: replace dead raise after return with warning log in report exporter :: reports.py L42-L42
- [git-history] fix: use parameterized query to close sql injection in customer lookup :: orders.py L36-L36
- [git-history] fix: deploy hook without shell string concat (command injection) :: hooks.py L11-L11
- [git-history] fix: narrow exception handling in pagination parsing :: handlers.py L40-L40
