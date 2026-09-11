# CodeAudit Agent 常用任务（Windows Git Bash / macOS / Linux 通用）
# 注意：本文件必须使用 tab 缩进。

.PHONY: install test lint demo serve clean

## 安装可编辑模式 + 开发依赖（pytest 等）
install:
	pip install -e ".[dev]"

## 全量单元测试（零网络、零真实 LLM）
test:
	python -m pytest tests -q

## 静态检查（规则见 pyproject.toml 的 [tool.ruff]，需先 pip install ruff）
lint:
	ruff check .

## 离线全闭环演示：检测 -> verified Patch -> 生成单测 -> 三格式报告
demo:
	python demo/run_demo.py

## 启动 Web 服务（REST API + SSE 进度 + 演示页），浏览器打开 http://127.0.0.1:8000
serve:
	python cli.py serve --host 127.0.0.1 --port 8000

## 清理演示工作区与本地缓存（不动源码）
clean:
	rm -rf demo/.demo_work .pytest_cache .ruff_cache build dist
	find . -type d -name "__pycache__" -not -path "./.git/*" -exec rm -rf {} +
