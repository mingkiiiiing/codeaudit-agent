# 贡献指南

感谢关注 CodeAudit Agent。本文覆盖环境搭建、日常开发任务、提交与 PR 规范；并行开发与契约机制见 docs/06~09。

## 开发环境

前置：Python >= 3.11（主开发版本 3.13）、git。

```bash
git clone https://github.com/mingkiiiiing/codeaudit-agent.git
cd codeaudit-agent
pip install -e ".[dev]"        # 可编辑安装 + pytest / pytest-asyncio
```

可选工具：

```bash
pip install "ruff==0.12.*"     # 静态检查（版本与 CI 一致）
export GLM_API_KEY=sk-xxx      # 可选；不配置则全程离线（纯规则模式）
```

未配置 Key 时所有测试与演示照常工作（零网络、零真实 LLM），因此贡献不依赖任何账号。

## 日常任务（Makefile）

| 目标 | 命令 | 说明 |
|---|---|---|
| `make install` | `pip install -e ".[dev]"` | 安装可编辑模式与开发依赖 |
| `make test` | `python -m pytest tests -q` | 全量单测（零网络、零真实 LLM） |
| `make lint` | `ruff check .` | 静态检查（规则见 `pyproject.toml [tool.ruff]`） |
| `make demo` | `python demo/run_demo.py` | 离线全闭环演示，应正常退出（退出码 0） |
| `make serve` | `python cli.py serve ...` | 启动 Web 服务（REST API + SSE + 演示页） |
| `make clean` | — | 清理演示工作区与本地缓存 |

改动后最低验证组合：`make test` 全量零回归 + `make lint` 零告警；触碰审计流水线时追加 `make demo`。

## 分支与提交规范

- **分支**：从 `main` 拉出，命名 `<type>/<短横线短述>`，如 `feat/sarif-output`、`fix/sandbox-timeout`、`docs/cli-reference`。
- **提交**：遵循 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/)，格式 `<type>: <中文描述>`（与仓库现有历史一致），常用 type：
  - `feat` 新功能｜`fix` 缺陷修复｜`docs` 文档｜`test` 测试｜`refactor` 重构｜`chore` 杂项｜`ci` 工作流
  - 示例：`feat: SARIF 2.1.0 输出与 --check 门禁`；一次提交聚焦一件事。

## 测试要求

- 全量 `python -m pytest tests -q` **零回归**（新功能必须带单测，与被改代码同 PR）；
- `ruff check .` **零告警**；新增代码不享有既有 per-file 豁免（见 `pyproject.toml [tool.ruff.lint.per-file-ignores]`，那些只保护存量违例）；
- 单元测试放在 `tests/unit/<模块>/`，与现有目录结构对齐；测试必须零网络、零真实 LLM 调用；
- 涉及流水线行为的改动，`python demo/run_demo.py` 必须保持退出码 0。

## 并行契约开发流程

本项目按 Wave 多任务并行，核心机制是**契约冻结 + 独占目录**（详见 docs/06、docs/07、docs/08、docs/09 的「协作规约」节）：

1. **先读契约**：每轮 Wave 的总体方案（docs/07~09）包含冻结的数据结构 / CLI 退出码 / 文件格式契约，改动涉及契约字段时先申请评审，不私改；
2. **只动自己的目录**：任务分解表为每个任务划定了独占目录/文件，越界改动会在 PR 审查中被拒绝；公共禁改区（如 `.github/workflows/ci.yml`、既有测试用例、进度事件文案关键字）见各 Wave 规约；
3. **文档随代码走**：新增 CLI 参数 / 配置项需同步 `docs-site/cli.md` 与 CHANGELOG；影响用户行为的改动需更新 README；
4. **集成人合入**：契约由集成人先行落地并冻结，各任务基于冻结契约开发，合入前由集成人统一联调。

## Pull Request 清单

提交 PR 前（模板中同款）：

- [ ] 全量测试零回归：`python -m pytest tests -q` 通过
- [ ] 静态检查零告警：`ruff check .` 通过
- [ ] 新增 / 变更行为附带单测（如适用）
- [ ] 文档同步更新（README / docs-site/ / CHANGELOG，如适用）
- [ ] 触碰流水线时离线演示可跑通：`python demo/run_demo.py` 退出码 0
- [ ] 未越出本任务的独占目录，未触碰禁改区与契约文件

## 发布流程（维护者）

1. 更新 `CHANGELOG.md`（Keep a Changelog 格式）；
2. 打 tag 推送：`git tag v0.x.y && git push origin v0.x.y` → `release.yml` 自动创建 GitHub Release（自动生成 Release Notes；PyPI 发布后续接入）；
3. 文档站由 `docs.yml` 在 main 分支文档变更时自动构建发布到 gh-pages（首次需在 Settings → Pages 将 Source 设为 `gh-pages` 分支 / root）。
