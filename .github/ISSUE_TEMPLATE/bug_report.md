---
name: Bug 报告
about: 报告误报、漏报、崩溃或与文档不符的行为
title: "[Bug] 简要描述问题"
labels: ["bug"]
assignees: []
---

<!-- 提交前请先搜索既有 issue，避免重复；误报 / 漏报请附规则 ID 与最小复现代码片段。 -->

## 环境

- [ ] 版本：<!-- `codeaudit --version`；未安装入口点时填 `git log -1` 的 commit hash -->
- [ ] Python 版本：<!-- `python --version`，如 3.13.x -->
- [ ] 操作系统：<!-- 如 Windows 11 / Ubuntu 22.04 -->
- [ ] 运行模式：<!-- 离线纯规则（未配置 GLM_API_KEY）/ LLM 双通道 -->

## 命令与复现

- [ ] 执行命令：<!-- 完整命令行，如 `python cli.py run ./demo/mini_app --no-llm` -->

  ```
  （粘贴完整命令）
  ```

- [ ] 复现步骤：

  1.
  2.

- [ ] 期望行为：
- [ ] 实际行为：

## 日志与输出

- [ ] 控制台报错 / 关键日志：

  ```
  （粘贴文本或截图；涉及报告内容请附 report.json 对应片段，可自行脱敏）
  ```

## 补充

- [ ] 可稳定复现（每次执行都能触发）
- [ ] 已删除 `.codeaudit/` 工作区后重试，问题仍存在（排除缓存干扰）
