"""Stage5 修复闭环（W2-A1；W7-A3 JS/TS 闭环）：编排入口 run_fix_stage。

对外 API（契约 v1.2 §3.2：编排层 config.do_fix 时调用 audit.fix.run_fix_stage）：
- run_fix_stage(ctx)：修复闭环编排（Python/JS/TS Issue 通用：按 severity 选 Issue，
  语法重解析由 tree-sitter 支持 js/ts，现有测试探测 pytest/jest/node-test）；
- build_fix_messages / generate_patch / validate_diff / apply_diff：补丁生成与应用（patcher）；
- syntax_ok / find_existing_tests / run_existing_tests：语法与测试验证（verifier）。
"""

from audit.fix.patcher import apply_diff, build_fix_messages, generate_patch, validate_diff
from audit.fix.stage import run_fix_stage
from audit.fix.verifier import find_existing_tests, run_existing_tests, syntax_ok

__all__ = [
    "run_fix_stage",
    "build_fix_messages",
    "generate_patch",
    "validate_diff",
    "apply_diff",
    "syntax_ok",
    "find_existing_tests",
    "run_existing_tests",
]
