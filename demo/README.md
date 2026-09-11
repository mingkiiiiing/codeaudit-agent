# demo —— 离线全闭环演示

本目录提供一个**不需要网络、不需要 GLM_API_KEY** 的完整演示：对迷你靶项目跑通
「问题检测 → 修复 Patch 沙箱验证（verified）→ 回归单测生成并通过 → 三格式审计报告」。

```
demo/
├── run_demo.py      # 一键演示脚本（python demo/run_demo.py）
├── mini_app/        # 演示靶项目：store.py 埋入 1 个 SQL 拼接注入缺陷（critical）
│   ├── README.md    # 靶项目说明（非真实业务代码）
│   ├── store.py     # 缺陷靶点：find_user 以字符串拼接构造 SQL
│   ├── textutil.py  # 干净的辅助模块
│   ├── main.py      # 入口
│   └── tests/test_store.py   # 现有测试（修复前后都应通过，用于 Patch 验证）
└── .demo_work/      # 每次运行自动重建的临时目录（已 gitignore）
```

## 三条命令

| 场景 | 命令 | 说明 |
|---|---|---|
| 离线演示（推荐先跑） | `python demo/run_demo.py` 或 `make demo` | 零网络、零 Key；LLM 被脚本替代，验证与沙箱运行全部真实执行 |
| 真实 LLM 运行 | `python cli.py run demo/mini_app --fix --tests --review-mode tools --out ./reports` | 需要 `GLM_API_KEY`（见根目录 `.env.example`），走 rule+llm 双通道 + Verify 复核 |
| 服务模式 | `python cli.py serve --host 127.0.0.1 --port 8000` 或 `make serve` | 浏览器打开 http://127.0.0.1:8000，用 Web 页创建任务、看进度与报告 |

## 离线演示的原理

- 靶项目每次运行前复制到 `demo/.demo_work/`，`demo/mini_app` 原件不会被修改（幂等）。
- 审计配置关闭 LLM 审查与复核（`enable_llm_review=False`、`enable_verify=False`），检测阶段走纯静态规则通道；
  `PY-SQL-INJECTION` 规则命中 `store.py:6`，形成唯一一条 critical 问题。
- 演示脚本用一个**按调用方身份分发**的脚本化 LLM（`DemoScriptedLLM`）替换流水线的 LLM 客户端：
  Fix Agent 的调用返回参数化修复的 unified diff，TestGen Agent 的调用返回 pytest 用例，其余调用返回空。
  注入方式是替换 `audit.orchestrator.pipeline._make_llm` 这一装配缝隙，`audit` 包本身零改动。
- 被脚本替代的只有 LLM 的"思考"；`git apply --check/apply`、tree-sitter 语法重解析、沙箱运行现有测试、
  生成单测的沙箱运行、报告渲染都是真实执行。因此 `verified` 与 `passed` 是跑出来的，不是写死的。

## 预期输出摘要

运行约 10 秒，六个步骤依次输出：

1. 靶项目文件清单与 `store.py` 源码（标出缺陷行）；
2. 七阶段流水线的进度事件（index 统计、detect 结果、fix 的生成/应用/语法/测试/终态、testgen 的生成/通过、报告落盘）；
3. 检测摘要：`critical=1 high=0 medium=0 low=0`，`ISS-0001 [critical/security] store.py:6`；
4. `PATCH-0001 修复 ISS-0001 apply_status = verified 现有测试 2/2 通过` + 完整 diff；
5. `TC-0001 目标 store.py:find_user status = passed`，打印生成文件内容，并在沙箱重放：`5 passed`；
6. `report.json / report.md / report.html` 三个路径。

结尾行：`Patch 状态：verified   生成单测：通过`，随后提示配置 `GLM_API_KEY` 后可跑真实 LLM 全流程。
脚本在未达到 verified/passed 时返回非零退出码，便于 `make demo` 作为门槛使用。

## 截图位

> 以下为占位框，录屏或截图后替换为图片（建议 `demo/screenshots/*.png`）。

```
┌──────────────────────────────────────────────────────────────┐
│  截图 1：步骤 1~2 —— 靶项目缺陷代码 + 七阶段进度事件           │
│                                                              │
│                        （此处放终端截图）                     │
└──────────────────────────────────────────────────────────────┘
```

```
┌──────────────────────────────────────────────────────────────┐
│  截图 2：步骤 4 —— Patch diff 与 verified 验证结果              │
│                                                              │
│                        （此处放终端截图）                     │
└──────────────────────────────────────────────────────────────┘
```

```
┌──────────────────────────────────────────────────────────────┐
│  截图 3：步骤 5~6 —— 生成单测 5 passed + 报告路径              │
│                                                              │
│                        （此处放终端截图）                     │
└──────────────────────────────────────────────────────────────┘
```

## 常见问题

- **提示 git 不可用 / Patch 落到 needs-review**：修复阶段依赖本机 `git apply`，请确认 `git` 在 PATH 中。
- **单测被剔除（dropped）**：沙箱用 `python -m pytest` 运行，请确认当前解释器已安装 `pytest`（`pip install -e ".[dev]"`）。
- **健康分为 0**：健康分按每千行加权问题密度计算，迷你项目基数极小，一条 critical 即压到 0 分，属口径正常。
