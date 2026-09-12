# 更新日志

本项目所有显著变更记录在此文件。

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.3.0] - 2026-09-12

Wave 5 + Wave 6：质量攻坚与健壮性清偿、规则库扩充、依赖约束治理。Wave 5 三路只读审查 + Dogfood 自审计共产出 68 项发现，本版修复其中 25 项核心问题，并新增联调测试套件（34 用例）与压力测试基线（2000 文件档纯规则审计 0.245 s/KLOC）；Wave 6 完成 R4 复核清偿（含 4 项必修缺陷）、静态规则库 49 → 63 条并上线内置规则手册、依赖约束治理。

### Added

- **规则库扩充至 63 条**：静态规则库 49 → 63 条（Python 35 / JavaScript 21 / TypeScript 专属 7），新增 14 条规则（Python 8、JavaScript 与 TypeScript 6），每条均附正反例测试；新增**内置规则手册** [docs-site/rules.md](docs-site/rules.md)——由 `python scripts/gen_rule_docs.py` 从规则注册表自动生成，含全部规则的判定说明与统计，规则与文档不再漂移。

### Fixed

- **密钥规则复数形态漏报（上版回归）**：0.2.1 引入的标识符分词精确匹配未覆盖复数形态，`API_KEYS` / `dbPasswords` / `credentials` 等命名全部漏报；token 归一化补齐复数形式后重新命中，并附复数形态正反例回归测试。
- **增量索引陈旧缓存**：索引预取缓存在同进程多次构建间未重置，改动 import 后的增量构建仍按陈旧依赖解析符号；构建开始时统一清空缓存，并补「改 imports 二次构建」回归用例。
- **删除 / 重命名补丁回滚**：补丁备份与生效性校验此前只覆盖 `+++ b/` 侧路径，删除型与重命名型补丁回滚不完整；现按 diff 两侧路径并集处理，覆盖全部补丁形态。
- **LLM 审查生效修复（critical）**：simple 审查模式下回调参数错绑，导致 LLM 通道完全不参与审查、报告却按「规则 + LLM」口径呈现；修复后 simple 模式真实生效。
- **修复 Patch 静默跳过**：嵌套 git 仓库场景下 `git apply` 可能在用户原始项目的外层仓库执行而被静默跳过；补丁的应用与回滚现严格限定在审计工作副本内。
- **SARIF 文件 uri 跨平台**：Windows 上生成的 SARIF `uri` 含反斜杠、不符合规范；统一归一为 `/` 分隔，各平台均可通过 upload-sarif 校验。
- **密钥规则误报**：PY-HARDCODED-SECRET 规则改为标识符分词精确匹配敏感词，并叠加熵 / 字符集随机性校验，普通命名常量（如内部指纹键名）不再被判为 critical。
- **服务端报告目录按任务隔离**：报告改写入 `<work-root>/<audit_id>/reports/`，并发或连续多次审计不再相互覆盖。

### Changed

- **understand 阶段随 ingest 门控**：ingest 失败时理解阶段与报告统计一并跳过，不再回读用户原始目录。
- **任务取消终态落盘**：客户端断开或任务取消（CancelledError）时任务以终态落盘并退出运行队列，不再悬挂为 running。
- **依赖约束治理**：完成第三方依赖与 CI Action 版本约束的收敛治理，降低供应链与构建漂移风险。
- **接入失败门控**：ingest 失败时 index / detect / fix / testgen 全部跳过并发「跳过：…（ingest 未成功）」事件，报告为空壳——绝不触碰用户原始目录、绝不向原项目写入补丁。
- **资源收口**：审计结束统一关闭 LLM 客户端与索引连接，长跑与批量场景不再泄漏连接。
- **服务端任务表有界化**：终态（已完成 / 失败）任务记录按 LRU 淘汰，服务长期运行内存不再无限增长。
- **门禁消息走 stderr**：`[门禁] 通过 / 未通过` 判定信息输出到 stderr，`--json --check` 组合下 stdout 为纯 JSON，可整体 `json.loads`。
- **`.codeaudit/` 默认忽略**：审计工作区目录加入流水线默认忽略清单（与 `.git`、`node_modules` 并列），对已含工作区的项目复跑不再产生嵌套副本与假问题，无需手动配置 gitignore。
- **程序名动态显示**：帮助 / usage 中的程序名按调用方式显示——`python cli.py` 显示 `cli.py`，安装态显示 `codeaudit`。
- **文档站与协作设施完善**：文档站导航收录设计文档 00、首页链接口径对齐；CLI 文档补全 `--fail-on` 单独指定即隐含启用门禁、程序名与门禁消息流向说明；README 区分离线基线（`bench.run`，零 Key 可跑）与真跑评估（`bench.real_run`，需 API Key）、注明 `.env` 不自动加载需手动 export、目录树补全 `scripts/` 与压测目录、Makefile 清单补 `clean`；离线 run 记录头部加注消融表配置数口径；CI / 压测调试残留路径统一纳入 `.gitignore` 与 ruff 排除。

### Security

- **超长行搜索降级**：除 zip 接入防护外，沙箱内的文件搜索对超长行自动降级处理，恶意构造的超长行不再拖垮审查进程。
- **zip 炸弹防护**：zip 接入增加 1 GB 累计解压上限，超限中止并明确报错，恶意超大压缩包不再拖垮进程。
- **测试目标注入防护**：run_tests 工具拒绝以 `-` 开头或越界的测试目标，阻断参数注入路径。

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

[Unreleased]: https://github.com/mingkiiiiing/codeaudit-agent/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/mingkiiiiing/codeaudit-agent/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/mingkiiiiing/codeaudit-agent/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/mingkiiiiing/codeaudit-agent/releases/tag/v0.1.0
