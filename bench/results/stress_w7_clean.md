# 压力测试与性能基线报告（bench/stress，W5-A2）

## 环境

- **生成时间**：2026-09-13 03:59:03
- **主机**：DESKTOP-KS9DBJD
- **CPU**：AMD64 Family 25 Model 117 Stepping 2, AuthenticAMD
- **平台**：Windows 11 (AMD64)
- **Python**：3.13.9
- **规模档位**：2000 文件
- **随机种子**：42
- **网络**：全程离线（FakeLLM / 纯规则模式，未配置也不读取 GLM_API_KEY）
- **数据目录**：D:\Project\代码库级智能审计与重构 Agent\bench\stress\data

口径说明：s/KLOC = 端到端 wall time / (源码行数/1000)，同 docs/04 §3.2；内存峰值为 tracemalloc 口径（单独跑，不计入耗时）；所有场景零网络。

## 摘要

| 规模 | ingest(s) | index build(s) | 文件 | 符号 | 调用边 | DB(MB) | 内存峰值(MB) | 规则审计 wall(s) | s/KLOC | 规则命中 |
|---|---|---|---|---|---|---|---|---|---|---|
| 2000 | 18.13 | 20.99 | 2000 | 17613 | 98893 | 28.61 | 44.3 | 73.02 | 0.35 | 41 |

## 场景明细：2000 文件档

### 场景 1（2000）：ingest + index ｜ 状态：OK ｜ 场景耗时 39.123s

| 指标 | 值 |
|---|---|
| files | 2000 |
| symbols | 17613 |
| call_edges | 98893 |
| resolved_edges | 17584 |
| resolved_ratio | 0.1778 |
| ingest_sec | 18.132 |
| index_build_sec | 20.991 |
| total_sec | 39.123 |
| materialize_mode | copy |
| db_size_mb | 28.605 |
| peak_memory_mb | 44.3 |
| peak_memory_note | tracemalloc 单独跑（Pass B），不计入耗时 |

### 场景 2（2000）：纯规则审计吞吐 ｜ 状态：OK ｜ 场景耗时 73.016s

| 指标 | 值 |
|---|---|
| wall_sec | 73.016 |
| loc | 207981 |
| sec_per_kloc | 0.351 |
| rule_hits | 41 |
| hits_fingerprint_sha | 4ad79f4656e1 |
| rule_scan_workers_requested | 0 |
| rule_scan_parallel | enabled=True；workers=4；files=2000 |
| issues | 41 |
| files_reviewed | 0 |
| pipeline_duration_sec | 63.031 |
| stages_events | ingest=3；index=3；understand=4；detect=4；refactor=3；fix=1；testgen=1；report=3；done=1 |
| loc_note | s/KLOC 口径同 docs/04 §3.2：wall / (loc/1000) |

### 场景 4（2000）：预算熔断 ｜ 状态：OK ｜ 场景耗时 80.031s

| 指标 | 值 |
|---|---|
| audit_completed | True |
| budget_tripped | True |
| llm_calls | 2001 |
| py_files | 2000 |
| calls_per_file | 1.0005 |
| review_errors | 2000 |
| rule_issues | 41 |
| llm_issues | 0 |
| partial_results | True |
| token_budget | 5000 |
| effective_per_file_budget | 500 |
| tokens_billed_prompt | 6003000 |
| tokens_billed_completion | 1000500 |
| wall_sec | 80.031 |
| done_event | True |
| issues_in_report | 41 |

### 场景 6（2000）：R1-8 索引 N+1 量化 ｜ 状态：OK ｜ 场景耗时 17.049s

| 指标 | 值 |
|---|---|
| call_edges | 98893 |
| profiled_build_sec | 24.641 |
| base_build_best3_sec | 19.2 |
| scenario1_base_build_sec | 20.991 |
| prefetch_build_best3_sec | 17.049 |
| measured_saving_pct | 11.2 |
| resolve_edges_cum_sec | 4.1372 |
| language_of_cum_sec | 0.1812 |
| aliases_cum_sec | 0.1822 |
| n_plus_one_share_of_resolve | 0.0878 |
| language_of_sql_calls | 98894 |
| aliases_sql_calls | 98894 |
| prefetch_edges_identical | True |
| estimate_note | n_plus_one_share 为 cProfile 估算上限；measured_saving_pct 为预取原型 best-of-3 实测 |

## 共享场景

### 场景 3：LLM 并发扩展性（200 文件 × sleep 0.2s × 并发 8） ｜ 状态：OK ｜ 场景耗时 5.185s

| 指标 | 值 |
|---|---|
| n_calls | 200 |
| concurrency | 8 |
| latency_sec | 0.2 |
| wall_sec | 5.1853 |
| ideal_wall_sec | 5.0 |
| serial_wall_sec | 40.0 |
| expansion_factor | 7.7142 |
| wall_over_ideal | 1.0371 |
| efficiency_pct | 96.4271 |
| review_errors | 0 |
| files_with_issues | 200 |
| ideal_note | 理想 wall = ceil(200/8) * 0.2s |

### 场景 5：server 并发（5 个小项目同时创建） ｜ 状态：OK ｜ 场景耗时 2.909s

| 指标 | 值 |
|---|---|
| tasks | 5 |
| mini_project_files | 12 |
| all_done | True |
| no_crosstalk | True |
| total_wall_sec | 2.909 |
| tasks_detail | {'audit_id': '5279cb068943', 'project': 'srv_mini_0', 'status': 'done', 'project_name_ok': True, 'issue_files_in_project': True, 'issues': 1}; {'audit_id': '3c5153e2ea9d', 'project': 'srv_mini_1', 'status': 'done', 'project_name_ok': True, 'issue_files_in_project': True, 'issues': 1}; {'audit_id': 'e74cbd7fdb8d', 'project': 'srv_mini_2', 'status': 'done', 'project_name_ok': True, 'issue_files_in_project': True, 'issues': 1}; {'audit_id': '3b1b936af88e', 'project': 'srv_mini_3', 'status': 'done', 'project_name_ok': True, 'issue_files_in_project': True, 'issues': 1}; {'audit_id': 'fe3c9629fb7b', 'project': 'srv_mini_4', 'status': 'done', 'project_name_ok': True, 'issue_files_in_project': True, 'issues': 1} |
| note | CPU 密集的规则扫描在同一事件循环内串行推进，总耗时≈各任务之和（并发价值在 IO/LLM 场景） |

## W7 优化对比（W6 基线 2026-09-12 vs W7 实测）

W7 改动：规则扫描按文件分片多进程并行（`rule_scan_workers=0` 自动：文件数 ≥ 100 时 min(4, cpu)）+ ingest 同盘硬链接物化（跨盘/失败回退拷贝）。下表 W6 列为基线摘录（串行规则扫描 + 全树复制），W7 列为本次真跑。

| 指标（2000 文件档） | W6 基线 | W7 实测 | 变化 | 说明 |
|---|---|---|---|---|
| ingest(s) | 8.335 | 18.132 | +117.5% | 越低越好 |
| index build(s) | 14.190 | 20.991 | +47.9% | 未改动（对照项） |
| 纯规则审计 wall(s) | 50.959 | 73.016 | +43.3% | 规则并行化主目标 |
| s/KLOC | 0.245 | 0.351 | +43.3% | docs/04 §3.2 口径 |
| 规则命中数 | 41 | 41 | +0.0% | 必须一致（correctness） |

**串行 vs 并行对拍（同一次生成项目、同规则集、同一负载窗口）**：

| 指标 | W7 串行（workers=1） | W7 并行（自动） | 规则阶段变化 |
|---|---|---|---|
| 纯规则审计 wall(s) | 78.595 | 73.016 | -7.1% |
| s/KLOC | 0.378 | 0.351 | — |
| 规则命中 | 41 | 41 | — |
| 命中指纹（md5 前 12 位） | 4ad79f4656e1 | 4ad79f4656e1 | — |

**命中集合一致校验：一致（PASS）**；并行信息：workers=4，files=2000，materialize=copy。

**ingest 物化 A/B（同负载窗口：硬链接路径 vs 整树复制路径）**：

| 指标 | 硬链接路径 | 复制路径（link_same_volume=False） | 变化 |
|---|---|---|---|
| ingest(s) | 18.132 | 10.646 | +70.3% |
| index build(s)（对照项） | 20.991 | 19.448 | — |

注意：本次实测 materialize=copy（work_dir 与源项目跨卷时 os.link 正确回退复制，硬链接收益需同卷 work_dir 才能体现；跨卷判定与回退本身符合设计）。

W6 基线来源：bench/results/stress_20260912.md（W5-A2/W6 压测，串行规则扫描 + 全树复制）。

## 结论

**吞吐结论**：2000 文件档纯规则审计 73.016s（207981 行 → 0.351 s/KLOC，满足 docs/04 §3.2 的 30 s/KLOC 目标），规则命中 41 条。

**LLM 并发扩展**：200 文件 × 并发 8 实测 wall 5.1853s，理想 wall（ceil(n/8)×0.2s）5.0s，扩展系数 7.7142（理论 8），wall/理想 = 1.0371——Semaphore 并发调度有效。

**预算熔断**（token_budget=5000，tools 路径按 1/10 下发=500）：2000 档：2001 次调用 / 2000 文件（1.0005 次/文件），review_errors 2000，熔断生效、部分结果收尾。任务全部正常完成（done 事件），未崩溃。

**R1-8 量化**：2000 档 98893 条调用边：base build 19.2s（best-of-3），_resolve_edges 占 4.1372s（cProfile 累计），其中 _language_of + _aliases 的 N+1 查询占其 8.8%；预取原型实测 17.049s（省 11.2%，边数一致=True）。

说明：上述占比是 cProfile 的累计口径（探针会放大逐调用开销），为节省上限估算；预取原型的 best-of-3 A/B 才是实测收益——SQLite 主键查询热缓存下本身很快，故实测省的比例显著低于占比。规模再上一个量级时 N+1 的线性开销占比会继续抬升。

建议：_resolve_edges 循环前一次性预取 files.language 映射，并按 caller 文件缓存 imports 别名表（可参照 bench/stress/run_stress.py 的 _PrefetchedIndexStore 原型），属低风险纯查询层优化。

**server 并发**：5 个不同小项目同时创建，全部 done=True，报告互不串扰=True，总耗时 2.909s（同循环 CPU 串行推进，与并发价值说明见场景明细）。

**瓶颈 Top3**（按最大规模档实测数据）：

1. **索引构建 N+1 查询（R1-8）**：index build 20.991s / 调用边 98893 条，_resolve_edges 内 8.8% 花在逐行 _language_of/_aliases SQL 上，预取实测可省 11.2%（best-of-3 A/B）。
2. **ingest 物化**：18.132s（含逐文件 sha256/清单/过滤；W7 起同盘硬链接物化，本次实测模式=copy）；ingest+index 合计 39.123s，占纯规则审计端到端的 54%——流水线前半段（工作副本物化 + 索引）仍是单次审计的固定串行开销，ingest 剩余成本以逐文件 sha256/解码为主（可选 fast 模式见 docs/12）。
3. **规则扫描并行化（W7 已启用）**：纯规则审计 wall 73.016s，规则命中 41 条；规则阶段按文件分片多进程并行（workers=4，files=2000），命中集合与串行版一致（见 W7 优化对比节）。剩余大头为 index build 与 ingest 的 sha256/解码。

