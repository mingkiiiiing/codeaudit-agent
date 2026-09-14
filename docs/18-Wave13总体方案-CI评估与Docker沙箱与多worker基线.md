# Wave 13 总体方案——CI 在线评估、Docker 沙箱、多 worker 基线与前端降级展示

> 2026-09-15 ｜ 状态：开发中 ｜ 前置：docs/17（W12）、v0.6.0 打版
>
> 用户指令：四个候选方向全部实施，token 预算不设限（仍需账目记录）。

## 1. 任务分解与目录所有权（四人并行 + 集成人收口）

| 任务 | 职务 | 所有权 | 交付物 | 验收 |
|---|---|---|---|---|
| W13-A1 | CI 工程师 | `.github/workflows/online-eval.yml`（新建）、`bench/eval/run_online_eval.py`、`.github/workflows/ci.yml`（如需联动） | 在线评估 CI 化：workflow_dispatch 手动 + schedule 每周一 UTC 0:00 定时；secrets.GLM_API_KEY 存在才跑（不存在跳过并标注）；单场景注入鲁棒性 quick（≤7 万 tokens/次）；评估 FAIL（判据不过）→ workflow 红 | bench/eval 增加 `--ci` 模式（简洁输出/非零退出码语义化）；workflow YAML 语法校验；本地模拟 CI 环境跑通守卫路径 |
| W13-A2 | 沙箱工程师 | `audit/sandbox/executor.py`、`audit/config.py`（如需开关）、`tests/unit/sandbox/` | Docker 沙箱后端：`docker run --rm --network none --memory 512m --cpus 1 -v <cwd>:/work -w /work <image> <cmd>`；启动/调用前探测 docker 可用性（which + `docker info` 探针，缓存探测结果）；**探测失败自动降级现有子进程路径并在 SandboxResult 标注 backend**（诚实降级哲学）；镜像默认 `python:3.12-slim`（可配 CODEAUDIT_DOCKER_IMAGE） | 单测全 mock docker CLI（可用/不可用/调用参数断言：network none、memory 限制、卷挂载）；本机无 Docker 的降级路径实测；既有 sandbox 用例全绿 |
| W13-A3 | 性能工程师 | `bench/stress/run_soak_multiworker.py`（新建）、`bench/results/` | 多 worker 压测基线：`--workers 2` 起真服务（cli.py serve --workers 2），单 worker 对照口径同 soak（混合负载/RSS/三态判定），额外观测：429 全局准入在多 worker 下的触发率、active 计数一致性、跨 worker SSE。产出 `bench/results/soak_multiworker_w13.md`（含单/多 worker 对照表） | 双 worker 600s（quick 300s）真实跑通；无 5xx；对照表完整；诚实边界（spawn 模式、sticky 执行） |
| W13-A4 | 前端工程师 | `frontend/src/`、`frontend/tests/`（如需） | 前端降级/预算可视化：①任务详情页——done 事件 `budget_tripped/degraded` → 醒目"预算熔断降级"警示条（含 used_tokens/token_budget）；②token 消耗卡片（stats 已有数据：llm_calls/prompt/completion）；③任务列表行降级标记（列表 API 需暴露的标记由现有 report 字段推导，若需后端字段改动改为前端从 /summary 推导，**不改 server/ 契约**） | vitest 新增组件测试；`npm run build` 通过；类型检查零错误 |
| W13-A5 | 集成人（主线） | `docs/`、`CHANGELOG.md`、`README.md`、`Makefile`、GitHub Secrets/Release、`mkdocs.yml` | v0.6.0 打版收尾（Release notes）；GitHub Secrets 配置 GLM_API_KEY；docs/18；派发收口；全量验收；commit | 六关 + 前端 build + CI 绿 |

**协作规约**：沿袭 W12。W13-A1 是唯二允许真实调 GLM 的任务（CI 抽样属运行期行为，开发期只验证守卫路径）。

## 2. 集成收口清单（W13-A5）

1. 全量 pytest + ruff + 前端 build/vitest。
2. 三路复跑（adversarial / soak / canary）。
3. A1 交付复核：workflow 语法（actionlint 或 YAML parse）、守卫路径本地实测、Secrets 配置确认（gh secret set，值不回显）。
4. GitHub Release v0.6.0 发布（notes 从 CHANGELOG 提取）。
5. commit + push + 盯 CI 全绿。

## 3. 风险

- Docker 后端在本机不可实测（无 Docker）——单测全 mock + 降级路径实测；真实 Docker 行为标注"待有 Docker 环境/CI 实证"，不虚构通过。
- schedule 定时任务烧 token（每周 ~7 万）——workflow_dispatch 为主、schedule 可由 repo 变量 ONLINE_EVAL_SCHEDULED=true/false 控制，默认 false。
- 多 worker 在 Windows spawn 下的端口/信号行为差异——如实记录，失败不改判定口径。
