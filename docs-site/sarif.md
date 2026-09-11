# SARIF 上传 GitHub Security tab

CodeAudit 支持 `--format sarif` 生成 **SARIF 2.1.0** 格式报告（文件名 `report.sarif`，与 JSON / Markdown / HTML 并存写入报告输出目录）。上传后问题会原生出现在仓库的 **Security → Code scanning** 页：按规则 / 严重度分组、点开可看代码行定位与描述，支持 branch 保护告警。

!!! tip "公共仓库免费"

    `github/codeql-action/upload-sarif@v3` 对**公共仓库免费**（GitHub Advanced Security 的 code scanning 能力对开源开放），私有仓库需要 GHAS 许可。

## 工作流示例

与 semgrep 官方推荐的同款两步结构：第一步跑审计产生 SARIF，第二步上传。复制为 `.github/workflows/security-scan.yml` 即可用：

```yaml
# Security Scan：CodeAudit 审计 + SARIF 上传 GitHub Security tab。
# 公共仓库免费（code scanning 对开源开放）；报告默认写入 <out>/report.sarif。
name: Security Scan

on:
  push:
    branches: [main]
  pull_request:
  schedule:
    - cron: "23 3 * * 1"   # 每周一定时全量扫一次，捕捉新规则发现的历史问题

permissions:
  contents: read
  security-events: write   # upload-sarif 需要 security-events 写权限

jobs:
  codeaudit:
    name: CodeAudit SARIF
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Python 3.13
        uses: actions/setup-python@v5
        with:
          python-version: "3.13"
          cache: pip

      - name: Install CodeAudit
        run: pip install -e .

      - name: Run CodeAudit（SARIF 输出）
        # 纯规则离线模式：CI 无需 GLM_API_KEY，零网络请求
        run: codeaudit run . --no-llm --format sarif

      - name: Upload SARIF to GitHub Security tab
        uses: github/codeql-action/upload-sarif@v3
        with:
          sarif_file: .codeaudit/reports/report.sarif
```

!!! note "路径约定"

    报告输出目录默认为 `<work-root>/reports`，`--work-root` 默认 `.codeaudit/`，因此上例取 `.codeaudit/reports/report.sarif`；用 `--out` 自定义了输出目录时请同步修改 `sarif_file`。

## 与 CI 门禁的关系

SARIF 上传与 `--check` 门禁是两条独立链路，可同时使用：

- **SARIF 上传**：只展示不拦截，问题进 Security tab 供人工分诊（如需在 PR diff 上呈现告警，可配合 `--diff origin/main` 只上报增量）；
- **`--check --fail-on <sev>`**：硬门禁，达到阈值直接让 job 失败。

注意：同一个 job 里启用 `--check` 会让审计步骤以退出码 `3` 失败、跳过上传步骤。若既要上传又要门禁，请拆成两个 job，或在审计步骤加 `continue-on-error: true` 后再单独判断退出码。
