# 更新日志

本项目所有显著变更记录在此文件。

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

## [0.2.0] - 2026-09-11

Wave 4：开源生态对标（SARIF / CI 门禁 / 增量审计 / 配置文件 / 打包入口 / 协作与文档设施）。

### Added

- **SARIF 输出**：`--format sarif` 生成 SARIF 2.1.0 报告 `report.sarif`，配合 `github/codeql-action/upload-sarif@v3` 可上传 GitHub Security tab（公共仓库免费）；报告契约新增 `schema_version` 字段。
- **CI 门禁**：`--check --fail-on <sev>`（`critical|high|medium|low`），命中阈值时退出码 `3`；退出码约定固化为 `0` 完成 / `1` 运行错误 / `2` bench 缺 key / `3` 门禁失败。
- **增量审计**：`--diff <git-ref>` 只审相对 ref 发生变更的文件，面向 PR 场景。
- **基线抑制**：`--report-baseline` 生成存量问题基线（问题指纹 sha1），`--baseline` 加载基线抑制命中项并计入 `stats.suppressed`；行内忽略 `# codeaudit: ignore[规则ID]`。
- **配置文件**：支持 `.codeaudit.toml` 与 `pyproject.toml [tool.codeaudit]` 自动发现，优先级 CLI > 环境变量 > 配置文件 > 默认值，未知键 / 非法值输出警告（`config_warnings`）。
- **入口点**：`pip install -e .` 后提供 `codeaudit` 命令，版本号单源（`pyproject.toml` 与 `audit.__version__` 一致）。
- **GitHub 协作设施**：issue / PR 模板、CODEOWNERS、Dependabot（pip 与 github-actions 每周巡检）、`release.yml`（tag 触发自动建 Release）与 `docs.yml`（文档站自动发布 gh-pages）。
- **文档站**：mkdocs-material 站点（`docs-site/`），含 CLI 全参数速查、SARIF 上传实践、PR 增量审计实践与 Roadmap；设计文档 01~09 由构建期镜像收录。

### Changed

- README 收口：新增「数据隐私」「CI 门禁与增量审计」「配置文件」「Roadmap」节，徽章区补 Release / Docs，安装节补 `codeaudit` 入口用法。

## [0.1.0] - 2026-09-11

Wave 1~3：核心流水线、检测与修复能力、产品化入口与评估基建。

### Added

- **七阶段流水线**：Ingest（路径 / zip、语言识别）→ Index（tree-sitter 符号表 + 调用图，SQLite）→ Understand（map-reduce 架构卡片）→ Detect（双通道）→ Fix（Patch 生成 + 沙箱验证）→ TestGen（pytest 生成 + 沙箱运行 + 失败重试）→ Report。
- **双通道检测**：tree-sitter AST + 正则静态规则库 49 条（覆盖 bug / performance / security / style，支持 Python / JavaScript / TypeScript）负责高召回；LLM Review Agent（上下文注入、`find_references` 等工具取证）+ Verify Agent 复核负责高精确；未配置 `GLM_API_KEY` 自动降级纯规则离线模式。
- **修复与单测闭环**：critical / high 生成 unified diff，经 `git apply --check`、语法重解析、运行项目现有测试三重验证，通过才标记 `verified`；对目标函数生成回归单测并在沙箱运行，失败带 traceback 重试。
- **报告三格式**：JSON / Markdown / HTML + 健康分（加权问题密度）；单阶段失败只记入报告不中断审计。
- **三端入口**：CLI（`run / index / report / serve`）、REST API（异步任务 + SSE 进度 + 仪表盘）、单文件 Web 演示页。
- **评估基准**：240 条金标（10 个项目集）、匹配与消融脚本、效率对比；离线纯规则基线实测 Precision(critical+high) 1.000 / Recall 0.844 / P50 5.0 s/KLOC（见 `bench/results/`；LLM 通道指标待真跑）。
- **工程化**：650 项单元测试全绿（Wave 3 收口基线）、GitHub Actions CI（Python 3.11 / 3.13 × ubuntu / windows 矩阵）、离线全闭环演示 `python demo/run_demo.py`。

[Unreleased]: https://github.com/mingkiiiiing/codeaudit-agent/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/mingkiiiiing/codeaudit-agent/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/mingkiiiiing/codeaudit-agent/releases/tag/v0.1.0
