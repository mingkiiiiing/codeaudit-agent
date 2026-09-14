# Wave 12 总体方案——在线 GLM 时代：安全治理、评估体系与遗留清偿

> 2026-09-14 ｜ 状态：开发中 ｜ 前置：docs/16（W11 持久化与多 worker）、在线 GLM 已接入
>
> 本 Wave 的背景变化：真实 GLM Key 已接入（.env，已 gitignore），项目从"纯离线工具"
> 变成"在线智能体"。W12 阶段一审查（2026-09-14）确认了在线形态下的新风险面与评估空白，
> 本方案按审查发现组织任务。

## 1. 阶段一审查结论（2026-09-14）

### 1.1 基线状态

- 全量 pytest 1123 passed + ruff 全绿（探针临时目录 `.llm_probe/` 曾污染 ruff 检查，
  已清理——教训进 §3 A2 契约：评估工具临时区必须放系统临时目录且强制清理）。
- W11 已推送（`6ed7bbd..ee0d136`，GitHub 网络恢复）；adversarial / soak / canary 三路
  W11 全部通过（前轮验收）。
- **A8 在线版注入鲁棒性实测：通过**——注入 payload（伪造 SYSTEM 指令/结束标记/工具输出）
  零生效、零回显；真缺陷全部保留（store.py critical、slugify 死循环 high）；payload 自身
  的 SQL 注入被 rule+llm 双通道确认；LLM 另独立发现 2 个 low。离线 vs 在线对照：
  离线 1 critical / 在线 1 critical(rule+llm) + 1 high(llm) + 2 low(llm)。
- **在线成本/延迟基准（首次建立）**：干净 151.6s / 7 调用 / 6296+22326 tokens；
  注入 147.4s / 9 调用 / 7587+22566 tokens（mini_app，5 文件）。
- Key 泄漏面静态审查：错误消息 / 日志 / 报告 / 事件流均不含 Key 值（仅 Authorization
  header 与不含值的 ConfigError 文案）——除 F6。

### 1.2 本 Wave 登记的新发现

| 编号 | 发现 | 位置 | 危害 | 状态 |
|---|---|---|---|---|
| F6 | W11 任务持久化把 `AuditConfig` 经 `dataclasses.asdict` 整体序列化进 SQLite `config_json`，**含明文 API Key** | `server/app.py:243` | 本地 db 文件被读取即 Key 泄漏（备份/同步/多用户主机场景放大） | W12-A1 修复 |
| F7 | dotenv 未产品化：`.env` 已是事实上的配置载体，但项目不自动加载，"拿到 Key 即可用"体验断裂（README 声明 Roadmap） | `audit/config.py` from_env | 在线模式易用性；误配静默降级离线 | W12-A1 修复 |
| F8 | token 预算熔断（`token_budget`，默认 2M）从未在真实 LLM 下验证过（既有测试全走 FakeLLM） | `audit/orchestrator/pipeline.py` | 在线模式预算失控风险未被实证覆盖 | W12-A3 修复 |
| F9 | 每任务 ~0.15-0.27 MB RSS 缓爬（W11 memdiag 已证明不在库层）尚未在服务端层归因 | server 侧 | 长时运行内存增长（FIFO 兜底下有界，非紧急） | W12-A4 归因 |

遗留不修：F1（无鉴权任意路径审计）、F3（Windows 沙箱无文件系统隔离）——本地工具设计
边界，部署形态（鉴权代理 + Docker）时根治。

## 2. 任务分解与目录所有权（四人并行 + 集成人收口）

| 任务 | 职务 | 所有权（只许改这些路径） | 交付物 | 验收 |
|---|---|---|---|---|
| W12-A1 | 安全工程师 | `audit/config.py`、`server/app.py`、`tests/unit/server/`、`tests/unit/config/`（如无则新建） | F6：config_json 落库前脱敏（api_key → `"<redacted>"`，运行时 Key 一律来自进程 env/from_env，db 中的值仅作审计痕迹）；F7：from_env 自动加载 `.env`（零依赖手写解析：仅赋值 env 中不存在的键，真实环境变量优先；解析失败/缺文件静默跳过；仅设 GLM_ 前缀与已知键） | db 中任何路径都取不到明文 Key（新增扫描用例断言 db 全文无 Key）；.env 自动加载用例（真实环境变量优先级）；既有用例全绿 |
| W12-A2 | 评估工程师 | `bench/eval/`（新建）、`.gitignore`、`tests/unit/bench/`（如需） | `bench/eval/run_online_eval.py`：三场景产品化——①注入鲁棒性（W12 审查探针的自动化，注入版 vs 干净版对照，判据：真缺陷保留 / 注入零回显 / 无幻觉 critical）；②离线 vs 在线同语料 diff（issues 集合对照）；③成本基准（token/延迟/调用数）。约束：**必须显式 `--online` 才真实调 GLM**（缺 Key 或未开 flag 时打印说明退出 2）；临时工作区一律系统临时目录 + finally 清理（.llm_probe 教训）；报告进 `bench/results/online_eval_<date>.md` | quick 档真实跑通一次（≤2 次 GLM 审计）；离线 flag 路径零网络 |
| W12-A3 | 可靠性工程师 | `audit/orchestrator/pipeline.py`、`tests/unit/pipeline/`（或既有预算测试所在位置） | F8：预算熔断在线语义硬化——既有 token_budget 熔断逻辑审查 + 单测补强（mock LLM 累计 token 验证熔断点、熔断后阶段行为、报告标注）；真实在线冒烟：小预算（如 3000 tokens）实跑 mini_app 验证熔断触发与诚实降级（`--online` 类似 W12-A2 的显式开关或 env 门控，防 CI 烧 token） | 熔断路径单测全绿；在线小预算实测报告（熔断触发点、降级行为、实际消耗 ≤ 预算） |
| W12-A4 | 性能工程师 | `bench/stress/run_soak.py`、`bench/memdiag/` | F9 归因：10 分钟长窗 soak（RSS 采样 10s 粒度 + 任务数相关性）+ 服务端层逐项排查清单（_RUNNING/_TASKS/store 连接与游标/SSE 生成器/事件累积）逐项给证据；结论三选一：单点泄漏定位（给修复建议，不动 server）/ 有界增长（给上界公式）/ 需进一步隔离 | 报告 `bench/results/memdiag_w12.md`；soak 长窗判定沿用三态口径 |
| W12-A5 | 集成人（主线） | `docs/`、`CHANGELOG.md`、`README.md`、`Makefile`、`bench/adversarial/` | 契约、派发、收口联调、三路复跑、在线评估审查、修复、发布 | 五关 + 在线评估审查全绿后 commit |

**协作规约**：沿袭 W1–W11（契约先行 / 所有权隔离 / 全程离线默认 / 诚实降级 / 中文
docstring / ruff 零告警）。**A2/A3 是唯二允许真实调用 GLM 的任务**，且必须在提示词
与产物中记录 token 消耗；其余任务禁触网。

## 3. 集成收口清单（W12-A5）

1. 全量 pytest + ruff（注意收口期临时探针目录卫生，用后即删）。
2. 三路复跑：adversarial / soak / canary（canary 双端均注入独立 CODEAUDIT_DB_PATH 已内置）。
3. 在线评估审查：A2 的 quick 报告复核（判据合理性、token 账目）；A3 在线小预算实测复核。
4. F6 复验：对 W11 遗留 db（.codeaudit/audits.db）全文扫描确认无明文 Key 增量；说明存量
   db 的清理建议（重建或手动清理，不自动删用户数据）。
5. CHANGELOG / README（在线模式使用说明 + .env 自动加载行为）/ Makefile（eval 目标）收口。

## 4. 验收与发布

六关全绿（pytest / ruff / adversarial / soak / canary / online-eval-quick）→ commit。
W9+W10+W11+W12 四节累积于 CHANGELOG [未发布] 段，打版（建议 v0.6.0）与推送时机由用户确认。
