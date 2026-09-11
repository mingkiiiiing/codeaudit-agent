# Roadmap

来源：[docs/09 §6](design/09-Wave4总体方案-开源生态对标.md)（开源生态对标结论的后续规划）。以下功能**明确不在 0.2.0 范围内**，作为后续版本的演进方向：

- [ ] **Playground**：浏览器在线演示，上传小项目即刻查看审计报告，零安装体验
- [ ] **规则市场 / Registry**：社区规则包的分发、版本管理与质量评级
- [ ] **MCP Server**：把审计工具（检测 / 索引 / 报告）暴露为 Model Context Protocol 服务，供任意 Agent 调用
- [ ] **云端 PR 机器人**：在 PR 里评论 `@codeaudit-bot` 触发增量审计并回帖报告，免配置工作流
- [ ] **Java / Go 语言支持**：扩展 tree-sitter 语法解析与双通道规则库
- [ ] **Docker 沙箱**：以容器级隔离替代 subprocess 沙箱，提升修复验证与单测运行的安全边界

## 已交付

- **0.2.0**（本轮）：SARIF 输出、CI 门禁（`--check --fail-on`）、PR 增量审计（`--diff`）、基线抑制、配置文件、`codeaudit` 入口、GitHub 协作设施与文档站——见 [CHANGELOG](https://github.com/mingkiiiiing/codeaudit-agent/blob/main/CHANGELOG.md)
- **0.1.0**：七阶段流水线、双通道检测（静态规则 + LLM Agent）、修复与单测闭环、三端入口（CLI / API / Web）、240 条金标的评估基准

> **0.2.1（2026-09-12）**：质量攻坚波次完成——三路审查 68 项发现修复 25 项（含 git apply 外层仓库静默跳过、review_fn 参数错绑两个高危缺陷）、联调测试 34 用例、压力测试基线 0.245 s/KLOC。详见 CHANGELOG [0.2.1]。
