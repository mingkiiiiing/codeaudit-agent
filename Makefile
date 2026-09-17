# CodeAudit Agent 常用任务（Windows Git Bash / macOS / Linux 通用）
# 注意：本文件必须使用 tab 缩进。

.PHONY: install test lint audit-self demo serve web adversarial soak canary eval clean

## 安装可编辑模式 + 开发依赖（pytest 等）
install:
	pip install -e ".[dev]"

## 全量单元测试（零网络、零真实 LLM）
test:
	python -m pytest tests -q

## 静态检查（规则见 pyproject.toml 的 [tool.ruff]，需先 pip install ruff）
lint:
	ruff check .

## 本地一键自审计（W15-C，等价 .github/workflows/pr-audit.yml 核心命令）：
## 离线增量审计当前仓库相对 origin/main 的变更，critical 门禁 + SARIF 产物
audit-self:
	python cli.py run . --diff origin/main --fail-on critical --format sarif --out .codeaudit/reports

## 离线全闭环演示：检测 -> verified Patch -> 生成单测 -> 三格式报告
demo:
	python demo/run_demo.py

## 启动 Web 服务（REST API + SSE 进度 + 审计工作台/演示页），浏览器打开 http://127.0.0.1:8000
serve:
	python cli.py serve --host 127.0.0.1 --port 8000

## 构建审计工作台（React SPA）到 frontend/dist；serve 检测到 dist 时优先托管（需 node ≥ 18）
web:
	cd frontend && npm install && npm run build

## 对抗与滥用测试八场景（真实 HTTP + 沙箱取证，约 5 分钟，离线；报告进 bench/results/）
adversarial:
	python -m bench.adversarial.run_adversarial

## Soak 持续混合负载压测（默认 300s；120s 冒烟口径：python -m bench.stress.run_soak --quick）
soak:
	python -m bench.stress.run_soak --duration 300

## 金丝雀双版本回放（git worktree 取旧 tag 起双服务，归一化 diff；默认 v0.5.0 vs HEAD）
canary:
	python -m bench.canary.replay

## 在线 GLM 评估（注入鲁棒性/离线对照/成本基准；加 ARGS=--online 才真实调 GLM）
eval:
	python -m bench.eval.run_online_eval

## 清理演示工作区与本地缓存（不动源码）
clean:
	rm -rf demo/.demo_work .pytest_cache .ruff_cache build dist
	find . -type d -name "__pycache__" -not -path "./.git/*" -exec rm -rf {} +
