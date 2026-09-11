# CodeAudit Agent

**代码库级智能审计与重构 Agent** —— 输入一个项目目录（或 zip），自动完成「架构理解 → 问题检测 → 修复 Patch → 单元测试生成 → 审计报告」全流程。基座模型 GLM-5.3 Flash（OpenAI 兼容接口）；未配置 API Key 时自动降级为纯静态规则的**离线模式**，不发起任何网络请求。

## 特性

- **双通道检测**：tree-sitter AST + 正则的静态规则通道负责高召回、零成本；LLM Agent 通道以规则命中为线索、注入符号表与调用链上下文并跨文件取证，经 Verify Agent 复核负责高精确。覆盖 bug / performance / security / style 四类问题。
- **修复闭环**：对 critical / high 问题生成 unified diff，`git apply` 后在沙箱中语法重解析、运行项目现有测试，只有验证通过的 Patch 才标记 `verified`，其余回退 `needs-review`，不阻断流程。
- **单测生成**：对已验证 Patch 涉及的目标函数生成 pytest 用例并在沙箱运行，失败带 traceback 重试，仍失败则剔除；生成文件只写入 `<src>/tests/generated/`，可整目录删除。
- **三端入口与 CI 集成**：CLI、REST API（SSE 进度）、Web 演示页三种使用方式；SARIF 2.1.0 报告可上传 GitHub Security tab，`--check --fail-on` 把审计变成 PR 门禁，`--diff` 只审增量、基线豁免存量（0.2.0）。

## QuickStart

```bash
# 1. 安装（Python >= 3.11；0.2.0 起提供 codeaudit 命令）
pip install -e ".[dev]"          # 或 pip install -e . 只装运行时依赖

# 2. 配置 LLM（可选，跳过则为纯规则离线模式）
export GLM_API_KEY=sk-xxx        # 智谱开放平台 API Key，见 .env.example

# 3. 完整审计（--fix / --tests 开启修复与单测闭环）
codeaudit run ./my-project --out ./reports
codeaudit run ./my-project --fix --tests --review-mode tools

# 4. Web 服务（REST API + SSE 进度 + 演示页）
codeaudit serve --host 127.0.0.1 --port 8000
```

未安装入口点时，以上 `codeaudit` 均可等价替换为 `python cli.py`；全部参数见 [CLI 速查](cli.md)。

## 离线演示（约 10 秒）

```bash
pip install -e ".[dev]"
python demo/run_demo.py
```

不需要网络与 API Key。脚本对内置的迷你靶项目 `demo/mini_app`（含一处 SQL 拼接注入缺陷）跑完整七阶段流水线：检测命中 critical 问题 → 修复 diff 经 `git apply` / 语法重解析 / 运行现有测试验证为 `verified` → 为修复后的函数生成回归单测并跑通 → 输出 JSON / Markdown / HTML 报告。演示中只有 LLM 的"思考"由脚本化 FakeLLM 替代，验证与沙箱运行均真实执行。

## 数据隐私

代码内容**仅发送至用户自行配置的 LLM API 端点**（默认智谱开放平台，可通过 `GLM_BASE_URL` 更换）；本项目不设任何服务器、不收集任何代码与遥测数据。检测过程中发现的密钥在报告中打码。未配置 API Key 的离线纯规则模式全程零网络请求。

## 深入阅读

| 页面 | 内容 |
|---|---|
| [CLI 速查](cli.md) | run / index / report / serve 全参数表与退出码约定 |
| [SARIF 上传 Security](sarif.md) | `--format sarif` + upload-sarif 工作流，问题进 GitHub Security tab |
| [PR 增量审计实践](pr-review.md) | `--diff` + `--check` + 基线：「存量豁免、增量把关」 |
| [Roadmap](roadmap.md) | 后续版本演进方向 |
| [使用文档 00~09](design/00-项目总览.md) | 需求、架构、Prompt、评估方案、Wave 方案等设计文档全文（左侧「使用文档」导航 00~09 逐篇可读） |
