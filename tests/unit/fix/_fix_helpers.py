"""tests/unit/fix 共享工具：临时项目构造、样例 diff、PipelineContext 工厂。

全部离线：LLM 一律用 FakeLLMClient 脚本回放；git CLI 仅本机调用（无网络）。
注意所有样例文件统一 LF 换行写入，保证 git apply 与 diff 逐字节对齐。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from audit.config import AuditConfig
from audit.pipeline import PipelineContext
from audit.workspace import WorkspaceContext

# ---------------------------------------------------------------- 样例项目

APP_PY = """import logging

logger = logging.getLogger(__name__)


def divide(a, b):
    try:
        return a / b
    except:
        return None
"""

TEST_APP_PY = """import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import divide


def test_divide_normal():
    assert divide(4, 2) == 2


def test_divide_by_zero():
    assert divide(1, 0) is None
"""

# 修复裸 except：改为精确捕获 + 记录日志（app.py 第 6-10 行，hunk 计数 5→6）
BARE_EXCEPT_DIFF = (
    "diff --git a/app.py b/app.py\n"
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -6,5 +6,6 @@\n"
    " def divide(a, b):\n"
    "     try:\n"
    "         return a / b\n"
    "-    except:\n"
    "+    except Exception:\n"
    '+        logger.exception("divide failed")\n'
    "         return None\n"
)

# 上下文与真实文件不匹配的 diff（结构合法但 git apply 必然失败）
BAD_CONTEXT_DIFF = (
    "diff --git a/app.py b/app.py\n"
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1,3 +1,3 @@\n"
    " bogus_line_a\n"
    "-bogus_line_b\n"
    "+bogus_fixed\n"
    " bogus_line_c\n"
)

# 语法破坏型 diff：except 后接未闭合列表（应用后 tree-sitter 解析必失败）
SYNTAX_BREAK_DIFF = (
    "diff --git a/app.py b/app.py\n"
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -6,5 +6,5 @@\n"
    " def divide(a, b):\n"
    "     try:\n"
    "         return a / b\n"
    "-    except:\n"
    "+    except [\n"
    "         return None\n"
)

# 行为破坏型 diff：裸 except 改成 except ValueError（语法合法，但 1/0 不再被捕获）
WRONG_EXCEPT_DIFF = (
    "diff --git a/app.py b/app.py\n"
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -6,5 +6,5 @@\n"
    " def divide(a, b):\n"
    "     try:\n"
    "         return a / b\n"
    "-    except:\n"
    "+    except ValueError:\n"
    "         return None\n"
)

# demo_proj/app/services/orders.py 第 8-13 行：SQL 拼接改参数化（6→6）
SQL_DIFF = (
    "diff --git a/app/services/orders.py b/app/services/orders.py\n"
    "--- a/app/services/orders.py\n"
    "+++ b/app/services/orders.py\n"
    "@@ -8,6 +8,6 @@\n"
    " def get_order_summary(order_id, conn):\n"
    "     user = get_user(order_id)\n"
    '     name = user["name"]  # line 10: get_user 可能返回 None，未判空\n'
    '-    query = "SELECT * FROM orders WHERE id = " + order_id  # line 11: SQL 拼接\n'
    "-    cur = conn.execute(query)\n"
    '+    query = "SELECT * FROM orders WHERE id = ?"\n'
    "+    cur = conn.execute(query, (order_id,))\n"
    "     return name, cur.fetchall()\n"
)

# R4-3：删除型补丁（+++ /dev/null）——victim.py 整文件删除
VICTIM_PY = "VALUE = 42\n"

DELETE_VICTIM_DIFF = (
    "diff --git a/victim.py b/victim.py\n"
    "--- a/victim.py\n"
    "+++ /dev/null\n"
    "@@ -1,1 +0,0 @@\n"
    "-VALUE = 42\n"
)

# 删除 victim.py 后必然失败的现有测试（import 报错）——触发回滚路径
TEST_VICTIM_PY = """import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from victim import VALUE


def test_value():
    assert VALUE == 42
"""


# ---------------------------------------------------------------- 工厂函数


def write_file(path: Path, content: str) -> None:
    """以 UTF-8 + LF 写文件（Windows 下 write_text 默认会把 \\n 翻译成 CRLF）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def make_workspace(tmp_path: Path, files: dict[str, str]) -> WorkspaceContext:
    """按 {相对路径: 内容} 造工作副本，返回指向它的 WorkspaceContext。"""
    src = tmp_path / "src"
    for rel, content in files.items():
        write_file(src / rel, content)
    return WorkspaceContext(
        audit_id="fixtests", src_root=src, work_root=tmp_path, db_path=tmp_path / "index.db"
    )


def make_ctx(
    workspace: WorkspaceContext, llm: Any, emitter: Any, **config_overrides: Any
) -> PipelineContext:
    """构造 do_fix=True 的 PipelineContext（不自动注入 issues，由测试按需填充）。"""
    config = AuditConfig(source_path=str(workspace.src_root), do_fix=True, **config_overrides)
    return PipelineContext(config=config, workspace=workspace, llm=llm, emitter=emitter)


def llm_json(diff: str, rationale: str = "最小修复：仅改动问题相关行") -> dict[str, str]:
    """FakeLLM 脚本项：返回 Fix Agent 约定的 JSON 字符串。"""
    return {"content": json.dumps({"diff": diff, "rationale": rationale}, ensure_ascii=False)}


def one_line_diff(rel: str, old: str, new: str) -> str:
    """单行替换的合法 unified diff（用于多文件批量场景）。"""
    return (
        f"diff --git a/{rel} b/{rel}\n"
        f"--- a/{rel}\n"
        f"+++ b/{rel}\n"
        "@@ -1,1 +1,1 @@\n"
        f"-{old}\n"
        f"+{new}\n"
    )
