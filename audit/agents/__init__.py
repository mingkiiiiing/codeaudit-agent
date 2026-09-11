"""审计角色实现（T3 基础上 Wave 2 A3 增强）：Review（文件审查）与 Verify（证据核验）。

Wave 2 新增：make_tools_review_fn（工具取证式审查）、review_files_parallel（文件级并发
与批量小切片）、prompts（版本化 prompt 常量）。
"""

from audit.agents.prompts import PROMPT_VERSION, REVIEW_PROMPT_V2, VERIFY_PROMPT_V2
from audit.agents.review import (
    BATCH_GROUP_SIZE,
    BATCH_SMALL_FILE_LINES,
    REVIEW_MAX_ITERATIONS,
    REVIEW_SLICE_LINES,
    REVIEW_TOOL_NAMES,
    TOOLS_REVIEW_MAX_ITERATIONS,
    TOOLS_REVIEW_TOKEN_BUDGET_CAP,
    TOOLS_REVIEW_TOOL_NAMES,
    build_review_prompt,
    issues_from_payloads,
    make_tools_review_fn,
    review_file,
    review_files_parallel,
)
from audit.agents.verify import verify_issue

__all__ = [
    "BATCH_GROUP_SIZE",
    "BATCH_SMALL_FILE_LINES",
    "PROMPT_VERSION",
    "REVIEW_MAX_ITERATIONS",
    "REVIEW_PROMPT_V2",
    "REVIEW_SLICE_LINES",
    "REVIEW_TOOL_NAMES",
    "TOOLS_REVIEW_MAX_ITERATIONS",
    "TOOLS_REVIEW_TOKEN_BUDGET_CAP",
    "TOOLS_REVIEW_TOOL_NAMES",
    "VERIFY_PROMPT_V2",
    "build_review_prompt",
    "issues_from_payloads",
    "make_tools_review_fn",
    "review_file",
    "review_files_parallel",
    "verify_issue",
]
