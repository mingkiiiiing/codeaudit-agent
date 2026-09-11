# PR 增量审计实践

全量审计用于入库体检，但放进 PR 流程会遇到一个经典矛盾：**存量问题会阻塞每一个 PR**。semgrep 给出的王牌实践是「上报与门禁分离」+「只报该 PR 引入的问题」——本页说明如何用 CodeAudit 的四个 0.2.0 参数在 GitHub Actions 里落地同一策略。

## 核心策略：存量豁免、增量把关

| 关注点 | 手段 | 效果 |
|---|---|---|
| 存量问题 | `--report-baseline` 一次性生成基线文件 | 一次性把当前所有问题记为"已知存量" |
| 存量豁免 | `--baseline <file>` 在 CI 中加载基线 | 基线命中的问题被抑制，计入 `stats.suppressed`，不进报告也不触发门禁 |
| 增量把关 | `--diff origin/main` 只审变更文件 | 只有相对目标分支改动的文件进入审计范围 |
| 质量闸门 | `--check --fail-on <sev>` | 增量引入达到阈值的问题 → 退出码 `3` → PR 检查失败 |

基线按**问题指纹**匹配（`sha1(file | 行区间 | 类别 | 标题前 60 字)`），问题被修复或挪动位置后指纹失效，会重新作为新问题出现——基线只会缩小、不会悄悄积累。

## 一次性：生成存量基线

在仓库干净状态下（默认分支、当前问题全量记录）执行一次：

```bash
codeaudit run . --no-llm --report-baseline .codeaudit-baseline.json
```

把生成的 `.codeaudit-baseline.json` 提交进仓库。此后存量问题不再干扰任何 PR；随着存量被顺手修复、指纹失效，基线会自然收缩。

## 每个 PR：增量审计工作流

复制为 `.github/workflows/pr-audit.yml`：

```yaml
# PR Audit：只审相对 origin/main 的变更文件，存量用基线豁免，增量问题作硬门禁。
name: PR Audit

on:
  pull_request:

permissions:
  contents: read

jobs:
  audit:
    name: CodeAudit (diff + gate)
    runs-on: ubuntu-latest
    steps:
      - name: Checkout（全量历史）
        # --diff 需要 git 历史来计算变更文件，必须 fetch-depth: 0
        uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Set up Python 3.13
        uses: actions/setup-python@v5
        with:
          python-version: "3.13"
          cache: pip

      - name: Install CodeAudit
        run: pip install -e .

      - name: Incremental audit with gate
        # 纯规则模式零网络请求，CI 无需 GLM_API_KEY；
        # 增量命中 high 及以上问题 → 退出码 3 → 本检查失败，阻止合并
        run: codeaudit run . --no-llm --diff origin/main --baseline .codeaudit-baseline.json --check --fail-on high
```

要点：

- `fetch-depth: 0` 是硬前提，浅克隆下 `--diff origin/main` 拿不到变更文件清单；
- `--diff` 的 ref 按目标分支写死为 `origin/main`（fork PR 与分支 PR 均成立）；如工作流按目标分支动态化，可用 `${{ github.base_ref }}` 换算；
- 阈值建议从 `high` 起步：critical + high 是修复闭环的触发级别，medium / low 适合作为非阻塞的报告项而非门禁。

## 阈值与门禁的分寸

- **先松后紧**：新项目可直接 `--fail-on high`；存量重的项目先用 `--fail-on critical`，随基线收缩再收紧；
- **门禁与展示分离**：门禁（本页）负责拦增量，SARIF 上传（见 [SARIF 上传 Security](sarif.md)）负责全量可视，两者互不替代；
- **不要用门禁消灭报告**：被基线抑制的问题仍计入 `stats.suppressed`，审计报告会如实呈现存量规模，避免"灯下黑"。
