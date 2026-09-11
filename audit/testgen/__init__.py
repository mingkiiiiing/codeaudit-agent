"""Stage6 单测生成闭环（W2-A2）：编排入口 run_testgen_stage。

对外 API（契约 v1.2 §3.2：编排层 config.do_tests 时调用 audit.testgen.run_testgen_stage）：
- run_testgen_stage(ctx)：单测生成闭环编排（生成 → 校验 → 落盘 → 沙箱运行 → 重试/剔除）；
- select_targets / generate_tests / count_asserts / validate_code：目标选取与代码生成校验（generator）；
- generated_rel_path / write_generated / run_generated / remove_generated：落盘运行与剔除（runner）。
"""

from audit.testgen.generator import count_asserts, generate_tests, select_targets, validate_code
from audit.testgen.runner import generated_rel_path, remove_generated, run_generated, write_generated
from audit.testgen.stage import run_testgen_stage

__all__ = [
    "run_testgen_stage",
    "select_targets",
    "generate_tests",
    "count_asserts",
    "validate_code",
    "generated_rel_path",
    "write_generated",
    "run_generated",
    "remove_generated",
]
