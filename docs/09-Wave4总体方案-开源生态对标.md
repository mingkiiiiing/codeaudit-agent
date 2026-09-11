# 09 Wave 4 总体方案：开源生态对标与功能扩展

版本：v1.0 ｜ 日期：2026-09-11 ｜ 前置：docs/08（Wave 3 已交付并发布 GitHub，650 测试全绿，CI 首跑 success）

---

## 1. 对标调研结论（他们有 → 我们缺 → 本轮做什么）

调研对象：**ruff**（最流行 Python linter）、**semgrep**（知名代码审计）、**pr-agent**（AI 评审机器人）、GitHub 平台生态惯例。网络受限处已用搜索补充 SARIF/门禁事实。

| # | 他们的做法 | 我们的差距 | Wave 4 动作 | 归属 |
|---|---|---|---|---|
| 1 | semgrep `--sarif` → `codeql-action/upload-sarif` → GitHub Security tab 原生展示 | 无 SARIF | SARIF 2.1.0 渲染器 + 上传文档 | A1 |
| 2 | semgrep `--error` 门禁退出码；"上报与门禁分离"设计 | 无 CI 门禁 | `--check --fail-on <sev>`，命中退出码 3 | A1 |
| 3 | semgrep "只报该 PR 引入的问题" 增量模式 | 只能全量 | `--diff <git-ref>` 只审变更文件 | A2 |
| 4 | ruff/semgrep 基线与忽略（baseline、行内 ignore） | 无 | 基线文件 + `# codeaudit: ignore[ID]` | A2 |
| 5 | ruff `pyproject.toml [tool.ruff]` 配置驱动 | 只有 CLI 参数 | `[tool.codeaudit]` / `.codeaudit.toml` | 集成人（v1.4） |
| 6 | pr-agent `pip install` 即用（console_scripts）、配置文件驱动 | 无入口点 | `[project.scripts] codeaudit` + 版本单源 | A4 |
| 7 | pr-agent 隐私声明（代码只发用户配置的 LLM）；ruff 徽章阵列/TOC/多渠道安装 | 无 | README 收口：隐私节、徽章、配置示例、门禁用法 | A3 |
| 8 | GitHub 生态：Release/tag/CHANGELOG、issue/PR 模板、CODEOWNERS、CONTRIBUTING、SECURITY、Dependabot、Pages 文档站 | 全无 | 协作套件 + mkdocs 站点 + release workflow | A3 |
| 9 | （roadmap，本轮不做）Playground、规则市场、MCP server、云端 PR 机器人 | — | README Roadmap 节 + docs/09 §6 | A3 |

## 2. 目标与验收

| 目标 | 验收 |
|---|---|
| G-A 接入 GitHub Security | `report.sarif` 可被 SARIF 2.1.0 校验；文档给出 upload-sarif 工作流示例；`--check --fail-on high` 在含 high 问题时退出码 3 |
| G-B 增量与抑制 | `--diff HEAD~1` 只审变更文件；基线文件命中问题被抑制并计入 `stats.suppressed`；行内 `# codeaudit: ignore[规则ID]` 生效 |
| G-C 配置文件 | `.codeaudit.toml` 与 `pyproject [tool.codeaudit]` 自动发现，优先级 CLI > 文件 > 默认；未知键给警告 |
| G-D 发布工程 | `pip install -e .` 后 `codeaudit` 命令可用；`__version__` 单源 0.2.0；Release/docs workflow、协作模板、mkdocs 站点入仓 |
| 底线 | 全量测试零回归（基线 650）；ruff 零告警；推送后 CI 绿 |

## 3. 契约 v1.4（集成人先行落地，冻结）

```python
# audit/models.py
AuditReport.schema_version: str = "1.0"     # 报告格式版本
AuditStats.suppressed: int = 0              # 被基线抑制的问题数

# audit/config.py（5 个新字段 + 配置文件合成）
fail_on_severity: str = "off"   # off|critical|high|medium|low → --check 门禁阈值
baseline_path: str = ""         # 已知问题基线；命中指纹的问题被抑制
report_baseline_out: str = ""   # 审计结束后把当前问题写入该基线文件
diff_ref: str = ""              # git ref（HEAD~1/origin/main...）→ 仅审变更文件
config_file: str = ""           # 显式配置文件；空则自动发现 .codeaudit.toml / pyproject [tool.codeaudit]
config_warnings: list[str]      # 配置文件未知键/非法值警告（只读输出字段）
@classmethod from_sources(cls, cli_overrides: dict | None, config_file: str = "", search_root=None) -> AuditConfig
# 合成优先级：默认值 < 配置文件 < 环境变量(GLM_*) < CLI 显式参数

# audit/utils.py
issue_fingerprint(issue) -> str   # sha1("file|line_start|line_end|category|title[:60]")，基线匹配的唯一依据
```

基线文件格式（A1 写、A2 读，双方按此实现）：
```json
{"schema_version": 1, "created_at": "...", "fingerprints": ["<sha1>", ...]}
```

退出码约定（A1 实现、全员文档引用）：`0` 完成（含发现问题但未启门禁）｜`1` 运行错误｜`2` bench 真跑缺 key（已占用）｜`3` `--check` 门禁失败。

## 4. 任务分解表（4 个大任务，全部并行）

| 代号 | 任务名 | 独占目录/文件 |
|---|---|---|
| W4-A1 | CI 门禁与 SARIF | `cli.py`、`audit/report/sarif.py`（新）、`tests/unit/cli/`、`tests/unit/report/` 下新文件 |
| W4-A2 | 抑制/基线/增量 | `audit/confkit.py`（新）、`audit/detect/engine.py`、`audit/orchestrator/pipeline.py`、tests 下新文件 |
| W4-A3 | GitHub 协作与发布套件 | `.github/**`（新增，不动 ci.yml）、`CONTRIBUTING.md`、`SECURITY.md`、`CHANGELOG.md`、`mkdocs.yml`、`docs-site/**`、`requirements-docs.txt`、`README.md` |
| W4-A4 | PyPI 打包与版本工程 | `pyproject.toml`（[project]/[project.scripts]/package-data）、`audit/__init__.py`、`scripts/check_build.py`、`tests/unit/pkg/**` |

README 收口归 A3：A1/A2/A4 在交付报告里提供各自功能的 README 片段（用法/示例），A3 负责合入。

## 5. 协作规约（继承 docs/06~08）

1. 契约 v1.4 落地后冻结；发现缺陷走报告申请。
2. A2 的 pipeline 改动不得改变既有事件文案关键字（Web 阶段进度判定依赖）。
3. A3 不动 `.github/workflows/ci.yml` 与既有测试；A4 的 pyproject 改动不得破坏 [tool.ruff]/[tool.pytest] 节与依赖列表（只允许追加/完善 [project] 元数据）。
4. 底线门槛：全量 pytest 零回归（基线 650）+ `ruff check .` 零告警 + `python demo/run_demo.py` exit 0。

## 6. Roadmap（写入 README，明确本轮不做）

Playground（在线演示）、规则市场/Registry、MCP Server（把审计工具暴露给任意 Agent）、云端 PR 机器人（评论 @bot 触发）、Java/Go 语言支持、Docker 沙箱。—— 这些是后续版本与面试"未来规划"素材。
