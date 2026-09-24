# Wave 25 总体方案：审计 P0 第三轮清偿（口径固化 / 模型 fallback / resume 全接线）

> 依据：第五轮审计报告 `bench/results/audit_round5_20260918_复审.md`（71/100 B）P0 清单。
> 工作轮路由：第五轮审计（提示词一）完成 → 同对话工作轮（提示词二）。
> 执行方式：三卡并行后台代理 + 集成人统一联调（与 W23/W24 同款流程）。

## 1. 范围裁决

| 审计 P0 项 | 本轮处置 | 理由 |
|---|---|---|
| P0-10 bench 官方口径固化 | ✅ 卡 A | 半小时级、无依赖、防"伪回归污染对外引用" |
| P0-7 模型路由 + fallback | ✅ 卡 B | 纯离线可开发可测试（429 注入），架构面清偿 |
| P0-9 resume 全接线（CLI sweep 自愈 + server 端点） | ✅ 卡 C | 第五轮新发现 F5-R2 的闭环修复 |
| P0-1 成本对数与翻默认 | ⏸ 跳过 | 09-18 单点探针实测 HTTP 429，在线真跑无意义；等配额窗口后跑 `python -m bench.w22_focus_compare` |
| safe-rename / Go 语言包 / 结果版本化 | 顺延 | 下轮工作轮候选（依赖本轮收口） |

## 2. 卡片设计（文件所有权互不相交）

- **卡 A = P0-10**：`bench/run.py`（--goldset 默认 goldset.jsonl 仓库根锚定 + --offline 开关透传 config_overrides；--ablation --offline 同步 base_overrides）、README「指标复现」节、tests/unit/bench/test_bench_run.py。
- **卡 B = P0-7**：`audit/llm/router.py` 新建（RouterClient：主模型 LLMError 后备模型一次机会；GLM_MODEL_FALLBACK / AuditConfig.fallback_model 默认空零变化；fallback_used 计数 + 主备统计求和；ConfigError 不切换）、`audit/config.py` 字段 + from_env env 层、`pipeline.py` `_make_llm` 构造点条件接线、tests/unit/llm/test_router.py。
- **卡 C = P0-9**：`cli.py`（`_resume_guard_action`/`_resume_sweep_grace_seconds` 共享纯函数 + cmd_resume running 分支 sweep 自愈）、`server/app.py`（POST /api/audits/{audit_id}/resume，404/409 语义，复用 _launch_audit 后台执行）、tests（CLI 3 + server 7 + gray_release 冻结断言同步）。
- 契约文件（models.py / indexer/base.py / llm/base.py）零改动；taskstore 状态机语义零改动（只调用）。

## 3. 集成人联调记录

- 改动面核对：16 文件 +909/-90（相对 bb471c0 含 W24-E），与三卡申报逐项吻合、无越权文件。
- 卡 C 自查修正 1 处：server resume 分支必须向 run_audit 传 audit_id（决定 task_root，不传则断点产物失效）——该问题在 server 用例首跑暴露并当场修复（happy path 断言倒逼）。
- 统一联调终验（最终树）：
  - 全量 pytest **2176 passed, 2 skipped**（296.85s）＝基线 2152 + 净新增 24（卡A 4 / 卡B 10 / 卡C 13 含冻结断言），逐位对账成立；
  - ruff 全域绿（audit cli.py server bench）；
  - demo 闭环 verified（8.4s）；
  - 金标终树复跑：`python -m bench.run --projects <10项目> --offline` 一条命令直出 **P=1.000 / R=0.844 / F1=0.916**、金标 240 条（`bench/results/w25_goldset_20260918.md`）——存量零回退门成立，且验证卡 A 固化口径在最终树生效；
  - resume SIGKILL 真进程 E2E：构造 running 滞留任务（stage_done=ingest、updated_at 回拨 60s）→ `codeaudit resume` → sweep 自愈 → 续跑 done（跳过 ingest，问题总数 8 与 Java 语料命中交叉一致）；
  - soak 300s 压测：四项指标判定见 `bench/results/soak_w10.md` 顶部记录（工具默认输出位）。

## 4. 已知边界（如实申报）

1. 卡 B：fallback 触发时的 warning 事件未接编排层（观测入口=RouterClient.fallback_used 属性，接事件流留给调用方轮次）；`GLM_MODEL_FALLBACK` 只接 from_env（server/bench 路径），CLI/配置文件路径（from_sources）未扩。
2. 卡 C：自愈 sweep 为全局清扫（CLI/server 同口径），grace 窗口内静默超过窗口的真活跃任务理论上可能被误判——窗口机制的既定取舍；resume 端点未加 429 准入检查（闸门信号量兜底排队）。
3. understand 阶段在 api_key='' 时仍尝试 LLM 并打降级警告（F5-R3 观感问题，未在本轮范围，留待后续小卡）。

## 5. 下一轮入口

- 工作轮候选（按优先级）：①P0-1 对数（GLM 配额窗口可用时第一时间跑 `python -m bench.w22_focus_compare`，判据 P50≤300 s/KLOC 且 P 降幅≤3pp 后决定 llm_focus 翻默认）；②Go 语言包（复制 Java 卡模式，resolved_ratio≥0.4 + E2E 双门）；③safe-rename 确定性原语 MVP（python 单语言，dogfood 20 符号 ≥18 verified）；④结果版本化（report_json 历史表 + diff 消费）；⑤F5-R3 FakeLLM 警告观感小卡。
- 待用户：W24-E + W25 全部工作区改动验收提交（bb471c0 之上）、push、打版 0.7.0。
