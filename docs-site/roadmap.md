# Roadmap

来源：[docs/09 §6](design/09-Wave4总体方案-开源生态对标.md)（开源生态对标结论的后续规划）。以下功能**明确不在 0.2.0 范围内**，作为后续版本的演进方向：

- [ ] **Playground**：浏览器在线演示，上传小项目即刻查看审计报告，零安装体验
- [ ] **规则市场 / Registry**：社区规则包的分发、版本管理与质量评级
- [ ] **MCP Server**：把审计工具（检测 / 索引 / 报告）暴露为 Model Context Protocol 服务，供任意 Agent 调用
- [ ] **云端 PR 机器人**：在 PR 里评论 `@codeaudit-bot` 触发增量审计并回帖报告，免配置工作流
- [ ] **Java / Go 语言支持**：扩展 tree-sitter 语法解析与双通道规则库

## 已交付

- **0.6.x**：Wave 13——CI 在线评估工作流（weekly 抽样 + 手动触发，GLM_API_KEY 缺失自动跳过）；**Docker 沙箱后端**（容器级隔离，opt-in 默认关闭，`CODEAUDIT_SANDBOX_BACKEND` 启用，docker 不可用时诚实降级 subprocess）；多 worker 压测基线（[bench/results/soak_multiworker_w13.md](https://github.com/mingkiiiiing/codeaudit-agent/blob/main/bench/results/soak_multiworker_w13.md)）；前端预算熔断 / 降级可视化——见 [CHANGELOG](https://github.com/mingkiiiiing/codeaudit-agent/blob/main/CHANGELOG.md)

- **0.6.0**：Wave 9–12 四波合并交付——**测试体系补全**（W9）：对抗八场景滥用测试运行器（`make adversarial`：恶意 zip 军火库 / 参数滥用矩阵 / 任务洪泛 / SSE 竞态 / 210MB 上传 / 沙箱逃逸 / 提示注入）、算法不变量与灰度发布保障测试进 CI；**服务治理与灰度基建**（W10）：任务准入控制（并发上限 + 排队 + 429，契约 v2.1）、流式上传分块落盘（413 上限语义不变）、沙箱输出限量 + truncated 标记、金丝雀双版本回放对拍（`make canary`）、Soak 混合负载压测（`make soak`）；**形态演进**（W11）：任务持久化 SQLite WAL——服务重启后终态任务与报告仍可查询（契约 v2.2）、线程池执行、协作式取消、多 worker 实验特性（`--workers N`）；**在线 GLM 安全治理与评估体系**（W12）：真实 GLM 接入与评估套件（提示注入鲁棒性实测通过、成本基准）、内存归因结论「有界增长非泄漏」（F9）、预算熔断全局闸门（F8）、Key 落库脱敏（F6）、`.env` 自动加载（F7）——见 [CHANGELOG](https://github.com/mingkiiiiing/codeaudit-agent/blob/main/CHANGELOG.md)

- **0.5.0**：Wave 8 Web 前后端——**前端工作台 `frontend/`**（React 18 + TypeScript + Vite + Ant Design 5 三页式 SPA：仪表盘任务列表 / 新建审计（路径与 zip 双入口）/ 任务详情——SSE 实时进度、ECharts 健康分与严重度分布、问题过滤、修复补丁 diff、报告三格式下载）；**REST API 契约 v2**（health / 任务列表 / 删除 / zip 上传 / 重构方案 / 架构理解端点 + SPA 静态托管）；**Web 压测基线**（读端点突发 / 并发审计 / SSE 并发流 / zip 上传四场景，真实 HTTP 口径）；CI 前端 job（typecheck + vitest + build 三关）——见 [CHANGELOG](https://github.com/mingkiiiiing/codeaudit-agent/blob/main/CHANGELOG.md)

- **0.4.0**（本轮）：Wave 7 赛题合规收口——**重构方案生成器**（确定性启发式 + LLM 增强，报告新增「重构方案」章节，契约 v1.7 `RefactorProposal`）；**JS/TS 修复与单测验证闭环**（`node --check` 语法验证 + `node --test` 单测验证，node 不可用诚实降级）；**性能实验与开关化**——规则多进程并行 / ingest 硬链接作为可选开关交付（默认串行 / 复制，本机 2000 文件档实测负收益诚实回退，命中集合指纹一致，对比数据见 [bench/results/stress_w7_clean.md](https://github.com/mingkiiiiing/codeaudit-agent/blob/main/bench/results/stress_w7_clean.md)）；赛题合规矩阵 10 项最终状态落定——见 [CHANGELOG](https://github.com/mingkiiiiing/codeaudit-agent/blob/main/CHANGELOG.md)
- **0.3.0**：Wave 5 质量攻坚 + Wave 6 健壮性清偿与发布——68 项发现修复 25 项（含 git apply 外层仓库静默跳过、review_fn 参数错绑两个高危缺陷）、联调测试 34 用例、压力测试基线 0.245 s/KLOC；R4 复核清偿 4 项必修（密钥规则复数形态漏报回归、增量索引陈旧缓存、删除 / 重命名补丁回滚、understand 未随 ingest 门控）；静态规则库 49 → 63 条并上线[内置规则手册](rules.md)；依赖约束治理——见 [CHANGELOG](https://github.com/mingkiiiiing/codeaudit-agent/blob/main/CHANGELOG.md)
- **0.2.0**：SARIF 输出、CI 门禁（`--check --fail-on`）、PR 增量审计（`--diff`）、基线抑制、配置文件、`codeaudit` 入口、GitHub 协作设施与文档站——见 [CHANGELOG](https://github.com/mingkiiiiing/codeaudit-agent/blob/main/CHANGELOG.md)
- **0.1.0**：七阶段流水线、双通道检测（静态规则 + LLM Agent）、修复与单测闭环、三端入口（CLI / API / Web）、240 条金标的评估基准
