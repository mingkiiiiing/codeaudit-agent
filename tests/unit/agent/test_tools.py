"""T3 单测：10 个默认工具（schema 与 docs/03 §2 全量比对 + handler 行为）。"""

from __future__ import annotations

import json

import pytest

from audit.agent.tools import (
    LIST_MAX_RESULTS,
    build_default_tools,
    format_numbered_lines,
    validate_issue_payload,
)
from audit.models import Reference, Symbol
from audit.sandbox.executor import SandboxResult

FILE = "app/services/users.py"


# ---------------------------------------------------------------------- fixtures/helpers


class StubIndex:
    """最小 IndexStore 桩：只实现 tools 用到的方法。"""

    def get_symbol(self, name: str, file_hint: str | None = None) -> Symbol | None:
        if name == "get_user":
            return Symbol(
                id="S1", file=FILE, kind="function", name="get_user",
                line_start=9, line_end=12, signature="def get_user(uid):",
            )
        return None

    def all_symbols(self) -> list[Symbol]:
        return [
            Symbol(file=FILE, kind="function", name="get_user", line_start=9, line_end=12),
            Symbol(file=FILE, kind="function", name="find_by_email", line_start=15, line_end=19),
        ]

    def references(self, name: str, file_hint: str | None = None) -> list[Reference]:
        if name == "get_user":
            return [Reference(file="app/services/orders.py", line=88, snippet="u = get_user(uid)")]
        return []

    def call_chain(self, symbol_name: str, direction: str = "callees", depth: int = 1) -> list[str]:
        return [f"{FILE}:9 get_user -> {FILE}:10 _USERS"]

    def dependencies(self, target: str, direction: str = "imports") -> list[str]:
        return ["app/services/users.py"]


class StubSandbox:
    def __init__(self, result: SandboxResult) -> None:
        self.result = result
        self.calls: list[tuple] = []

    async def run_tests(self, framework: str, cwd, target: str = "") -> SandboxResult:
        self.calls.append((framework, str(cwd), target))
        return self.result


@pytest.fixture
def tools(sample_workspace):
    return {spec.name: spec for spec in build_default_tools(sample_workspace)}


def _issue_dict(sample_workspace, **overrides):
    total = sample_workspace.line_count(FILE)
    payload = {
        "category": "bug",
        "severity": "high",
        "title": "未判空",
        "file": FILE,
        "line_start": min(10, total),
        "line_end": min(11, total),
        "description": "描述",
        "suggestion": "建议",
        "confidence": 0.9,
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------- schema 全量比对（docs/03 §2）

EXPECTED_SCHEMAS = {
    "list_files": {
        "type": "object",
        "properties": {
            "prefix": {"type": "string", "description": "目录前缀，如 'app/services'"},
            "pattern": {"type": "string", "description": "glob 模式，如 '*.py'"},
        },
        "required": [],
    },
    "read_file": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对项目根的路径"},
            "start_line": {"type": "integer", "description": "起始行（1-based，含）"},
            "end_line": {"type": "integer", "description": "结束行（含），单次最大 200 行"},
        },
        "required": ["path"],
    },
    "search_code": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "正则表达式或普通文本"},
            "file_glob": {"type": "string", "description": "可选，限定文件范围如 'app/**/*.py'"},
            "is_regex": {"type": "boolean", "default": True},
        },
        "required": ["query"],
    },
    "get_symbol": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "符号名，如 'get_user' 或 'UserService.create'"},
            "file_hint": {"type": "string", "description": "可选，限定定义所在文件"},
        },
        "required": ["name"],
    },
    "find_references": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "file_hint": {"type": "string", "description": "定义所在文件，消歧同名符号"},
        },
        "required": ["name"],
    },
    "get_call_chain": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "direction": {"type": "string", "enum": ["callers", "callees"], "default": "callees"},
            "depth": {"type": "integer", "minimum": 1, "maximum": 2, "default": 1},
        },
        "required": ["symbol"],
    },
    "get_dependencies": {
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "文件路径或模块名"},
            "direction": {"type": "string", "enum": ["imports", "imported_by"], "default": "imports"},
        },
        "required": ["target"],
    },
    "record_issues": {
        "type": "object",
        "properties": {
            "issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "category": {"type": "string", "enum": ["bug", "performance", "style", "security"]},
                        "severity": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
                        "title": {"type": "string", "description": "一句话问题标题（中文）"},
                        "file": {"type": "string"},
                        "line_start": {"type": "integer"},
                        "line_end": {"type": "integer"},
                        "description": {"type": "string", "description": "为什么是问题、触发条件、后果（中文）"},
                        "evidence": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "证据，如 'orders.py:88 调用 get_user；users.py:41 return None'",
                        },
                        "suggestion": {"type": "string", "description": "修复建议（中文）"},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                    "required": [
                        "category", "severity", "title", "file", "line_start", "line_end",
                        "description", "suggestion", "confidence",
                    ],
                },
            }
        },
        "required": ["issues"],
    },
    "submit_patch": {
        "type": "object",
        "properties": {
            "issue_id": {"type": "string"},
            "diff": {"type": "string", "description": "unified diff，含 ---/+++ 头与 @@ hunk"},
            "rationale": {"type": "string", "description": "修复思路说明（中文，将写入报告）"},
        },
        "required": ["issue_id", "diff", "rationale"],
    },
    "run_tests": {
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "测试文件/用例路径，空为全部"},
            "framework": {"type": "string", "enum": ["pytest", "jest"]},
        },
        "required": ["framework"],
    },
}

EXPECTED_DESCRIPTIONS = {
    "list_files": "列出项目文件。可按目录前缀或 glob 模式过滤。返回相对路径列表。",
    "read_file": "读取文件内容，每行前带行号（'12: code'）。大文件自动截断到 200 行，返回值会提示剩余区间。",
    "search_code": "全库正则/文本搜索，返回 'file:line: 匹配行' 列表（最多 50 条）。用于确认某模式的所有出现位置。",
    "get_symbol": "按名称取代码符号（函数/类/方法）的定义：源码、签名、所在文件与行号。找不到时返回近似候选名。",
    "find_references": "查找符号在项目中的全部引用位置（调用点），基于预建调用图，未解析引用以搜索兜底。用于评估影响面与取证。",
    "get_call_chain": "查询某函数向上（谁调它）或向下（它调谁）两跳的调用链，输出 'A.f → B.g' 路径列表。",
    "get_dependencies": "查询文件或模块的导入依赖与被依赖关系。",
    "record_issues": "提交本文件审查结论。必须恰好调用一次：有问题提交问题数组，无问题提交空数组。行号必须来自你读到的文件内容，禁止猜测。",
    "submit_patch": "提交针对单个 Issue 的修复，unified diff 格式（git apply 可解析）。只允许改动与该 Issue 直接相关的行。",
    "run_tests": "在沙箱中运行测试（白名单：pytest / jest）。返回退出码与末尾 80 行输出。60 秒超时。",
}


def test_ten_tools_with_docs03_schemas_verbatim(tools):
    assert set(tools) == set(EXPECTED_SCHEMAS)
    for name, schema in EXPECTED_SCHEMAS.items():
        spec = tools[name]
        assert spec.parameters == schema, f"{name} 的 parameters 与 docs/03 §2 不一致"
        assert spec.description == EXPECTED_DESCRIPTIONS[name], f"{name} 的 description 与 docs/03 §2 不一致"
        # to_openai_tool 结构符合 OpenAI function calling 格式
        openai_tool = spec.to_openai_tool()
        assert openai_tool["type"] == "function"
        assert openai_tool["function"]["name"] == name
        assert openai_tool["function"]["parameters"] == schema
        assert openai_tool["function"]["parameters"] is spec.parameters  # 透传同一 schema 对象


# ---------------------------------------------------------------------- list_files


async def test_list_files_all_and_filtered(sample_workspace, tools):
    res = await tools["list_files"].handler()
    assert res["count"] == len(res["files"])
    assert "app/services/users.py" in res["files"]
    assert "main.py" in res["files"]

    res = await tools["list_files"].handler(prefix="app/services")
    assert "app/services/orders.py" in res["files"]
    assert "app/services/users.py" in res["files"]
    assert "main.py" not in res["files"]

    res = await tools["list_files"].handler(pattern="*.py")
    assert all(f.endswith(".py") for f in res["files"])
    assert "app/main.py" in res["files"] or "main.py" in res["files"]

    res = await tools["list_files"].handler(prefix="../escape")
    assert "error" in res


# ---------------------------------------------------------------------- read_file


async def test_read_file_numbered_and_total(sample_workspace, tools):
    res = await tools["read_file"].handler(path=FILE)
    assert res["total_lines"] == sample_workspace.line_count(FILE)
    assert res["content"].startswith("1: ")
    assert "def get_user(uid):" in res["content"]
    assert not res["truncated"]


async def test_read_file_window(sample_workspace, tools):
    res = await tools["read_file"].handler(path=FILE, start_line=9, end_line=12)
    assert res["start_line"] == 9
    assert res["end_line"] == 12
    assert "9: def get_user(uid):" in res["content"]


async def test_read_file_truncates_over_200_lines_with_hint(sample_workspace, tools):
    big = sample_workspace.src_root / "big.py"
    big.write_text("\n".join(f"line{i}" for i in range(1, 351)), encoding="utf-8")
    res = await tools["read_file"].handler(path="big.py")
    assert res["total_lines"] == 350
    assert res["truncated"] is True
    assert res["end_line"] == 200
    assert "截断" in res["content"] and "350" in res["content"]
    assert "start_line=201" in res["hint"]
    # 续读区间
    res2 = await tools["read_file"].handler(path="big.py", start_line=201, end_line=350)
    assert res2["truncated"] is False
    assert "350: line350" in res2["content"]


async def test_read_file_errors(sample_workspace, tools):
    res = await tools["read_file"].handler(path="no_such.py")
    assert "error" in res
    res = await tools["read_file"].handler(path="../outside.py")
    assert "error" in res and "非法路径" in res["error"]
    res = await tools["read_file"].handler(path="C:/Windows/win.ini")
    assert "error" in res
    res = await tools["read_file"].handler(path=FILE, start_line=99999)
    assert "error" in res and "超出范围" in res["error"]


# ---------------------------------------------------------------------- search_code


async def test_search_code_regex_and_glob(sample_workspace, tools):
    res = await tools["search_code"].handler(query=r"def get_user\(")
    assert res["count"] >= 1
    assert any(m.startswith(f"{FILE}:9:") for m in res["matches"])

    res = await tools["search_code"].handler(query="get_user", file_glob="app/services/*.py")
    assert all(m.startswith("app/services/") for m in res["matches"])

    # is_regex=False：按普通文本
    res = await tools["search_code"].handler(query="def get_user(uid)", is_regex=False)
    assert any(m.startswith(f"{FILE}:9:") for m in res["matches"])


async def test_search_code_errors_and_empty(sample_workspace, tools):
    res = await tools["search_code"].handler(query="(")
    assert "error" in res and "正则" in res["error"]
    res = await tools["search_code"].handler(query="  ")
    assert "error" in res
    res = await tools["search_code"].handler(query="不存在的字符串xyz")
    assert res["count"] == 0 and res["matches"] == []


# ---------------------------------------------------------------------- 索引类工具


async def test_index_tools_require_index(sample_workspace, tools):
    for name, kwargs in (
        ("get_symbol", {"name": "get_user"}),
        ("find_references", {"name": "get_user"}),
        ("get_call_chain", {"symbol": "get_user"}),
        ("get_dependencies", {"target": FILE}),
    ):
        res = await tools[name].handler(**kwargs)
        assert res == {"error": "index not built"}


async def test_get_symbol_with_index(sample_workspace, tools):
    index = StubIndex()
    specs = {s.name: s for s in build_default_tools(sample_workspace, index=index)}
    res = await specs["get_symbol"].handler(name="get_user")
    assert res["file"] == FILE and res["line_start"] == 9
    assert "def get_user(uid):" in res["source"]

    res = await specs["get_symbol"].handler(name="get_usr")  # 近似候选
    assert "error" in res
    assert "get_user" in res["candidates"]


async def test_references_call_chain_dependencies_with_index(sample_workspace, tools):
    index = StubIndex()
    specs = {s.name: s for s in build_default_tools(sample_workspace, index=index)}

    res = await specs["find_references"].handler(name="get_user")
    assert res["count"] == 1
    assert res["references"][0].startswith("app/services/orders.py:88")

    res = await specs["get_call_chain"].handler(symbol="get_user")
    assert res["direction"] == "callees" and res["depth"] == 1
    assert res["chains"] == [f"{FILE}:9 get_user -> {FILE}:10 _USERS"]

    res = await specs["get_call_chain"].handler(symbol="get_user", direction="up")  # 非法方向
    assert "error" in res

    res = await specs["get_dependencies"].handler(target=FILE)
    assert res["dependencies"] == ["app/services/users.py"]

    res = await specs["get_dependencies"].handler(target=FILE, direction="sideways")
    assert "error" in res


# ---------------------------------------------------------------------- record_issues


async def test_record_issues_accepts_valid_payload(sample_workspace, tools):
    res = await tools["record_issues"].handler(issues=[_issue_dict(sample_workspace)])
    assert res["ok"] is True and res["recorded"] == 1

    res = await tools["record_issues"].handler(issues=[])  # 无问题空数组也是合法提交
    assert res["ok"] is True and res["recorded"] == 0


async def test_record_issues_line_range_validation(sample_workspace, tools):
    total = sample_workspace.line_count(FILE)
    res = await tools["record_issues"].handler(
        issues=[_issue_dict(sample_workspace, line_start=0)]
    )
    assert "error" in res and "line_start" in json.dumps(res, ensure_ascii=False)

    res = await tools["record_issues"].handler(
        issues=[_issue_dict(sample_workspace, line_start=total + 1, line_end=total + 1)]
    )
    assert "error" in res and str(total) in json.dumps(res, ensure_ascii=False)

    res = await tools["record_issues"].handler(
        issues=[_issue_dict(sample_workspace, file="ghost.py")]
    )
    assert "error" in res and "ghost.py" in json.dumps(res, ensure_ascii=False)


async def test_record_issues_field_and_enum_validation(sample_workspace, tools):
    bad = _issue_dict(sample_workspace)
    del bad["suggestion"]
    res = await tools["record_issues"].handler(issues=[bad])
    assert "error" in res and "suggestion" in json.dumps(res, ensure_ascii=False)

    res = await tools["record_issues"].handler(issues=[_issue_dict(sample_workspace, category="code")])
    assert "error" in res and "category" in json.dumps(res, ensure_ascii=False)

    res = await tools["record_issues"].handler(issues=[_issue_dict(sample_workspace, severity=" urgent")])
    assert "error" in res

    res = await tools["record_issues"].handler(issues="不是数组")
    assert "error" in res


def test_validate_issue_payload_unit(sample_workspace):
    total = sample_workspace.line_count(FILE)
    assert validate_issue_payload(_issue_dict(sample_workspace), lambda p: total) is None
    assert validate_issue_payload("x", lambda p: total) is not None
    assert validate_issue_payload(_issue_dict(sample_workspace, line_start=total + 5), lambda p: total) is not None
    assert validate_issue_payload(_issue_dict(sample_workspace), lambda p: None) is not None  # 文件不存在


# ---------------------------------------------------------------------- submit_patch


async def test_submit_patch_returns_ack(tools):
    res = await tools["submit_patch"].handler(
        issue_id="ISS-0001",
        diff="--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,3 @@\n context\n-old\n+new",
        rationale="最小修复",
    )
    assert res == {
        "ok": True,
        "issue_id": "ISS-0001",
        "status": "received",
        "message": res["message"],
    }

    assert "error" in await tools["submit_patch"].handler(issue_id="", diff="@@ x", rationale="r")
    assert "error" in await tools["submit_patch"].handler(issue_id="I", diff="没有 hunk", rationale="r")
    assert "error" in await tools["submit_patch"].handler(issue_id="I", diff="@@ x", rationale="")


# ---------------------------------------------------------------------- run_tests


async def test_run_tests_requires_sandbox(tools):
    res = await tools["run_tests"].handler(framework="pytest")
    assert res == {"error": "sandbox not available"}


async def test_run_tests_whitelist(sample_workspace):
    specs = {s.name: s for s in build_default_tools(sample_workspace, sandbox=StubSandbox(SandboxResult()))}
    res = await specs["run_tests"].handler(framework="make")
    assert "error" in res and "pytest" in res["error"]


async def test_run_tests_delegates_to_sandbox(sample_workspace):
    stub = StubSandbox(
        SandboxResult(exit_code=0, stdout_tail="1 passed", stderr_tail="", duration_sec=1.2)
    )
    specs = {s.name: s for s in build_default_tools(sample_workspace, sandbox=stub)}
    res = await specs["run_tests"].handler(framework="pytest", target="tests/test_x.py")
    assert res["exit_code"] == 0
    assert res["stdout_tail"] == "1 passed"
    assert res["timed_out"] is False
    assert stub.calls == [("pytest", str(sample_workspace.src_root), "tests/test_x.py")]


# ---------------------------------------------------------------------- 小工具函数


def test_format_numbered_lines():
    assert format_numbered_lines(["a", "b"], start=2) == "2: a\n3: b"


async def test_all_handlers_never_raise(sample_workspace, tools):
    """handler 契约：任何输入都不抛异常。"""
    for name, spec in tools.items():
        try:
            await spec.handler()  # 无参调用
            await spec.handler(**{k: None for k in spec.parameters.get("properties", {})})
        except Exception as exc:  # pragma: no cover
            pytest.fail(f"工具 {name} 抛出了异常: {exc!r}")


# ---------------------------------------------------------------- R1-13 / R1-12 回归


async def test_run_tests_rejects_option_injection(sample_workspace):
    """R1-13：'-' 开头的 target（pytest 选项注入）被拒绝，不传给沙箱。"""
    stub = StubSandbox(SandboxResult())
    specs = {s.name: s for s in build_default_tools(sample_workspace, sandbox=stub)}
    res = await specs["run_tests"].handler(framework="pytest", target="-p no:cacheprovider")
    assert "error" in res and "禁止" in res["error"]
    res2 = await specs["run_tests"].handler(framework="pytest", target="--import-mode=importlib")
    assert "error" in res2
    assert stub.calls == []  # 沙箱从未被调用


async def test_run_tests_rejects_escape_and_outside_paths(sample_workspace):
    """R1-13：.. 越级 / 越出工作副本的绝对路径同样拒绝。"""
    stub = StubSandbox(SandboxResult())
    specs = {s.name: s for s in build_default_tools(sample_workspace, sandbox=stub)}
    assert "error" in await specs["run_tests"].handler(framework="pytest", target="../outside.py")
    assert "error" in await specs["run_tests"].handler(
        framework="pytest", target="C:/Windows/system32/cmd.exe"
    )
    assert stub.calls == []


async def test_run_tests_normalizes_absolute_path_inside_root(sample_workspace):
    """R1-13：src_root 内的绝对路径归一为相对路径后执行。"""
    stub = StubSandbox(SandboxResult(exit_code=0))
    specs = {s.name: s for s in build_default_tools(sample_workspace, sandbox=stub)}
    res = await specs["run_tests"].handler(
        framework="pytest", target=str(sample_workspace.src_root / "tests" / "test_x.py")
    )
    assert res["exit_code"] == 0
    assert res["target"] == "tests/test_x.py"
    assert stub.calls[-1][2] == "tests/test_x.py"


async def test_search_code_rejects_overlong_regex(sample_workspace):
    """R1-12：超过长度上限的正则 query 直接拒绝（防灾难性回溯）。"""
    specs = {s.name: s for s in build_default_tools(sample_workspace)}
    from audit.agent.tools import SEARCH_MAX_PATTERN_LEN

    res = await specs["search_code"].handler(query="a" * (SEARCH_MAX_PATTERN_LEN + 1))
    assert "error" in res and "过长" in res["error"]


# ---------------------------------------------------------------------- R4-8：超长单行降级


async def test_search_code_giant_line_falls_back_to_literal(sample_workspace, tools):
    """R4-8：超 64KB 单行不跑正则，降级为原始 query 的字面量子串匹配。"""
    giant = "x = '" + "a" * 70_000 + "NEEDLE_MARKER_9f8a7b';\n"
    src = sample_workspace.src_root
    (src / "giant_blob.py").write_text(giant, encoding="utf-8", newline="\n")

    # 字面量子串在超长行上仍可命中（该 query 作为正则同样合法，但超长行走降级路径）
    res = await tools["search_code"].handler(query="NEEDLE_MARKER_9f8a7b")
    assert any(m.startswith("giant_blob.py:1:") for m in res["matches"])

    # 正则语义在超长行上被放弃：query 只按字面量匹配，正则专属构造不命中
    res = await tools["search_code"].handler(query=r"N[EED]{2}LE")  # 字面量不在行内
    assert all(not m.startswith("giant_blob.py:") for m in res["matches"])

    # 短行不受影响：正则语义照常生效
    res = await tools["search_code"].handler(query=r"def get_user\(")
    assert res["count"] >= 1
