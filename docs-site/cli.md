# CLI 速查

!!! note "版本说明"

    标注 **0.2.0** 的参数为本轮 Wave 4 新增，功能代码与本文档并行交付，行为以契约 v1.4（[docs/09 §3](design/09-Wave4总体方案-开源生态对标.md)）为准；0.1.0 参数已在现网可用。

## 调用方式

`pip install -e .` 后使用 `codeaudit` 命令（0.2.0 起提供，版本号单源）；二者完全等价：

```bash
codeaudit run ./my-project --no-llm
# 等价于
python cli.py run ./my-project --no-llm
```

帮助与用法信息中的程序名按实际调用方式**动态显示**：`python cli.py …` 的 `--help` / usage 显示 `cli.py`，安装态 `codeaudit …` 显示 `codeaudit`，两种方式行为完全一致。

全局查看版本：`codeaudit --version`。

## 退出码约定

| 退出码 | 含义 |
|---|---|
| `0` | 完成。**含发现问题时**——只要未启用门禁（`--check` 或 `--fail-on`），发现多少问题都返回 0 |
| `1` | 运行错误：路径不存在、报告文件非法、运行期异常等 |
| `2` | bench 真跑缺 `GLM_API_KEY`（该码已被评估脚本占用，CI 场景不会出现） |
| `3` | 门禁失败（由 `--check` 启用，或单独指定 `--fail-on` 隐含启用）：问题严重度达到或超过阈值 |

## run —— 完整审计

```bash
codeaudit run <source_path> [参数]
```

| 参数 | 说明 |
|---|---|
| `source_path` | 待审计项目路径或 zip |
| `--out DIR` | 报告输出目录（默认 `<work-root>/reports`） |
| `--work-root DIR` | 工作区根目录（默认 `.codeaudit/`） |
| `--lang python javascript ...` | 限定语言，默认自动检测；支持 Python / JavaScript / TypeScript |
| `--fix` | 开启修复 Patch 生成（0.1.0） |
| `--tests` | 开启单测生成（0.1.0） |
| `--no-llm` | 禁用 LLM，纯规则模式（零网络请求） |
| `--review-mode simple\|tools` | LLM 审查模式：单次 JSON 调用 / 工具取证循环（默认 simple） |
| `--no-verify` | 关闭 Verify Agent 复核 |
| `--fix-max N` / `--testgen-max N` | 单次审计最多生成的 Patch 数 / 单测目标函数数（默认 50 / 30） |
| `--json` | 以 JSON 输出完整报告到 stdout（门禁消息一律走 stderr，`--json --check` 组合下 stdout 仍可整体 `json.loads`） |
| `--check` **0.2.0** | 启用 CI 门禁：命中阈值时退出码 `3`（详见 [PR 增量审计实践](pr-review.md)） |
| `--fail-on <sev>` **0.2.0** | 门禁阈值 `critical\|high\|medium\|low`（critical > high > medium > low，缺省 `high`）；**单独指定即隐含启用门禁**，无需同时给 `--check` |
| `--format sarif` **0.2.0** | 追加生成 SARIF 2.1.0 报告 `<out>/report.sarif`（与 JSON/Markdown/HTML 并存，见 [SARIF 上传 Security](sarif.md)） |
| `--diff <ref>` **0.2.0** | 只审相对 git ref（如 `HEAD~1`、`origin/main`）发生变更的文件；CI 中需 `fetch-depth: 0` |
| `--baseline <file>` **0.2.0** | 基线文件（问题指纹列表）；命中的存量问题被抑制并计入 `stats.suppressed` |
| `--report-baseline <file>` **0.2.0** | 审计结束后把当前问题写入该文件，作为后续 `--baseline` 的存量基线 |
| `--config <file>` **0.2.0** | 显式指定配置文件；缺省时自动发现 `.codeaudit.toml` / `pyproject.toml [tool.codeaudit]` |

配置合成优先级：**CLI 显式参数 > 环境变量（`GLM_*`）> 配置文件 > 默认值**；配置文件中的未知键 / 非法值以警告形式输出，不中断审计。

## index —— 只构建接入与索引（Stage 1~2）

```bash
codeaudit index <source_path> [--work-root DIR]
```

打印符号表 / 调用图统计，用于快速了解项目结构与索引性能。

## report —— 报告格式转换

```bash
codeaudit report <report_json> [--format md|html] [--out PATH]
```

把已落盘的 `report.json` 转成 Markdown / HTML；缺省 `--out` 时打印到标准输出。SARIF 的生成在 `run` 阶段（`--format sarif`），本子命令不涉及。

## serve —— Web 服务

```bash
codeaudit serve [--host 127.0.0.1] [--port 8000]
```

启动 FastAPI 服务：单文件演示页（`/`）、异步任务（`POST /api/audits`）、SSE 进度流、报告下载与仪表盘 API。完整端点清单见仓库 README「REST API 与 Web 演示页」节。

## 典型组合

```bash
# 本地全量审计（LLM 双通道 + 修复 + 单测）
codeaudit run ./my-project --fix --tests --review-mode tools --out ./reports

# CI 门禁：high 及以上问题存在则失败（退出码 3）
codeaudit run . --no-llm --check --fail-on high

# PR 增量审计：只审相对 origin/main 的变更，存量用基线豁免
codeaudit run . --no-llm --diff origin/main --baseline .codeaudit-baseline.json --check --fail-on high

# 生成报告 + SARIF 上传 GitHub Security tab
codeaudit run . --no-llm --format sarif
```
