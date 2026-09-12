"""Stage6 单测生成闭环（W2-A2；W7-A3 扩展 JS/TS）：编排入口 run_testgen_stage。

对外 API（契约 v1.2 §3.2：编排层 config.do_tests 时调用 audit.testgen.run_testgen_stage）：
- run_testgen_stage(ctx)：单测生成闭环编排（生成 → 校验 → 落盘 → 沙箱运行 → 重试/剔除；
  Python→pytest，JS/TS→node:test / node --test，node 不可用诚实跳过）；
- select_targets / generate_tests / count_asserts / validate_code / validate_code_js：
  目标选取与代码生成校验（generator）；
- js_import_specifier：生成的 .test.mjs 导入目标模块的相对说明符（generator）；
- generated_rel_path / write_generated / run_generated / remove_generated：落盘运行与剔除（runner）。
"""

from audit.testgen.generator import (
    count_asserts,
    generate_tests,
    js_import_specifier,
    select_targets,
    validate_code,
    validate_code_js,
)
from audit.testgen.runner import generated_rel_path, remove_generated, run_generated, write_generated
from audit.testgen.stage import run_testgen_stage

__all__ = [
    "run_testgen_stage",
    "select_targets",
    "generate_tests",
    "count_asserts",
    "validate_code",
    "validate_code_js",
    "js_import_specifier",
    "generated_rel_path",
    "write_generated",
    "run_generated",
    "remove_generated",
]
