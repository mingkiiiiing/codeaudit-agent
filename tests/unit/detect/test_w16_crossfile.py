"""W16 克隆检测（audit.detect.crossfile）单测：全离线，tmp_path 造项目。"""

from __future__ import annotations

from pathlib import Path

import pytest

from audit.detect import crossfile
from audit.models import Category, Issue, IssueSource, Severity


# ---------------------------------------------------------------- 最小 stub


class _StubWorkspace:
    """最小工作区 stub：source_files / rel / read_file_text 闭包 tmp 目录。"""

    def __init__(self, root: Path) -> None:
        self.root = root

    def source_files(self, languages=None):
        return sorted(p for p in self.root.rglob("*") if p.is_file())

    def rel(self, path) -> str:
        return Path(path).resolve().relative_to(self.root.resolve()).as_posix()

    def read_file_text(self, rel) -> str:
        return (self.root / rel).read_text(encoding="utf-8")


class _StubCtx:
    """最小 PipelineContext stub：仅携带 workspace 与 extra（防御式降级用）。"""

    def __init__(self, root: Path) -> None:
        self.workspace = _StubWorkspace(root)
        self.extra: dict = {}


@pytest.fixture
def make_proj_ctx(tmp_path):
    """按 {相对路径: 文本} 在 tmp_path 落盘并返回 stub ctx 的工厂。"""

    def _make(files: dict[str, str]) -> _StubCtx:
        for rel, text in files.items():
            target = tmp_path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        return _StubCtx(tmp_path)

    return _make


# ---------------------------------------------------------------- 用例素材

# 7 行函数体（不含 def 行），alpha/beta 两处重复 => 归一化后 7 行克隆块
_BODY_7 = """    total = 0
    for it in items:
        if it > 0:
            total += it
        acc(it)
        mark(it)
    return total"""


def _func(name: str, body: str) -> str:
    return f"def {name}(items):\n{body}\n"


# ---------------------------------------------------------------- 用例


class TestCrossfile:
    def test_same_file_clone_hit(self, make_proj_ctx):
        """同文件克隆：两个同体函数 => 命中 1 条，挂在第二处（后续）位置。"""
        text = (
            _func("alpha", _BODY_7)
            + "\n\n"
            + _func("beta", _BODY_7)
        )
        ctx = make_proj_ctx({"m.py": text})
        issues = crossfile.run(ctx)
        assert len(issues) == 1
        issue = issues[0]
        assert isinstance(issue, Issue) and issue.id == ""  # id 由 engine 统一补
        assert issue.category == Category.STYLE
        assert issue.severity == Severity.MEDIUM
        assert issue.source == IssueSource.RULE
        assert issue.confidence == pytest.approx(0.7)
        assert issue.file == "m.py"
        # 第一处：def alpha 在第 1 行，体 2~8；第二处：def beta 在第 11 行，体 12~18
        assert (issue.line_start, issue.line_end) == (12, 18)
        assert "第 12~18 行" in issue.title and "m.py 第 2~8 行" in issue.title
        assert "rule:PY-CLONE" in issue.evidence
        assert issue.suggestion
        assert issue.description

    def test_cross_file_clone_hit(self, make_proj_ctx):
        """跨文件克隆：a.py/b.py 同体函数 => 命中挂在 b.py（排序靠后的文件）。"""
        ctx = make_proj_ctx(
            {
                "a.py": _func("work_a", _BODY_7),
                "b.py": _func("work_b", _BODY_7),
            }
        )
        issues = crossfile.run(ctx)
        assert len(issues) == 1
        issue = issues[0]
        assert issue.file == "b.py"
        assert (issue.line_start, issue.line_end) == (2, 8)
        assert "a.py 第 2~8 行" in issue.title
        assert "rule:PY-CLONE" in issue.evidence

    def test_five_lines_not_reported(self, make_proj_ctx):
        """5 行重复（低于阈值 6）不报；两侧空行 padding 也不允许凑满阈值。"""
        body = "\n".join(f"    x{i} = {i}" for i in range(5))

        def func(name: str) -> str:
            return f"def {name}(items):\n{body}\n"

        ctx = make_proj_ctx({"m.py": func("one") + "\n\n" + func("two")})
        assert crossfile.run(ctx) == []

    def test_comment_and_whitespace_normalized_hit(self, make_proj_ctx):
        """注释与空白差异在归一化后抹平：仍命中。"""
        plain = _BODY_7
        noisy = (
            "    total = 0        # 初始化累计器\n"
            "    for it in items:\n"
            "        if  it  >  0:   # 只看正数\n"
            "            total += it\n"
            "        acc(it)   # 累加\n"
            "        mark(it)\n"
            "    return total\n"
        )
        ctx = make_proj_ctx({"m.py": _func("alpha", plain) + "\n\n" + _func("beta", noisy)})
        issues = crossfile.run(ctx)
        assert len(issues) == 1
        assert "rule:PY-CLONE" in issues[0].evidence

    def test_string_and_number_normalized_hit(self, make_proj_ctx):
        """字符串内容与数字字面量差异归一化为 STR/NUM 后仍命中。"""
        first = (
            "    total = 0\n"
            '    name = "alpha-1"\n'
            "    limit = 100\n"
            "    for it in items:\n"
            "        if it > limit:\n"
            "            total += it\n"
            "    return total\n"
        )
        second = (
            "    total = 0\n"
            '    name = "beta-2"\n'
            "    limit = 250\n"
            "    for it in items:\n"
            "        if it > limit:\n"
            "            total += it\n"
            "    return total\n"
        )
        ctx = make_proj_ctx({"m.py": _func("alpha", first) + "\n\n" + _func("beta", second)})
        issues = crossfile.run(ctx)
        assert len(issues) == 1
        assert "rule:PY-CLONE" in issues[0].evidence

    def test_no_clone_project_zero_hit(self, make_proj_ctx):
        """无重复项目 0 命中。"""
        ctx = make_proj_ctx(
            {
                "a.py": (
                    "def load(path):\n"
                    "    with open(path) as fh:\n"
                    "        return fh.read()\n"
                    "\n"
                    "def save(path, data):\n"
                    "    with open(path, 'w') as fh:\n"
                    "        fh.write(data)\n"
                    "    return True\n"
                ),
                "b.js": (
                    "export function tick(n) {\n"
                    "  let out = '';\n"
                    "  for (let i = 0; i < n; i++) {\n"
                    "    out += '.';\n"
                    "  }\n"
                    "  return out;\n"
                    "}\n"
                ),
            }
        )
        assert crossfile.run(ctx) == []

    def test_three_copies_dedup_same_second_start(self, make_proj_ctx):
        """同一段落归属多对时去重：三份拷贝只报 2 条（同一第二处 start 只报最优对）。"""
        text = (
            _func("f1", _BODY_7)
            + "\n\n"
            + _func("f2", _BODY_7)
            + "\n\n"
            + _func("f3", _BODY_7)
        )
        ctx = make_proj_ctx({"m.py": text})
        issues = crossfile.run(ctx)
        assert len(issues) == 2
        starts = sorted(issue.line_start for issue in issues)
        # f2 体起始 12 行；f3 体起始 22 行
        assert starts == [12, 22]

    def test_env_override_min_lines(self, make_proj_ctx, monkeypatch):
        """CODEAUDIT_CLONE_MIN_LINES 可下调阈值：5 行克隆在阈值 4 时命中。"""
        monkeypatch.setenv("CODEAUDIT_CLONE_MIN_LINES", "4")
        body = "\n".join(f"    y{i} = {i}" for i in range(5))

        def func(name: str) -> str:
            return f"def {name}(items):\n{body}\n    return 0\n"

        ctx = make_proj_ctx({"m.py": func("one") + "\n\n" + func("two")})
        issues = crossfile.run(ctx)
        assert len(issues) == 1

    def test_total_lines_guard_records_error(self, make_proj_ctx, monkeypatch):
        """总行数超上限：整体跳过并记 post_scan_errors，返回空列表。"""
        monkeypatch.setattr(crossfile, "MAX_TOTAL_LINES", 5)
        body = "\n".join(f"    z{i} = {i}" for i in range(10))
        ctx = make_proj_ctx({"m.py": f"def big(items):\n{body}\n"})
        assert crossfile.run(ctx) == []
        assert "超" in ctx.extra["post_scan_errors"]["audit.detect.crossfile"]

    def test_workspace_failure_degrades(self, tmp_path):
        """workspace 异常时降级：不抛异常，返回空列表并记账。"""

        class _BadWorkspace:
            def source_files(self, languages=None):
                raise RuntimeError("boom")

        class _BadCtx:
            workspace = _BadWorkspace()
            extra: dict = {}

        ctx = _BadCtx()
        assert crossfile.run(ctx) == []
        assert "audit.detect.crossfile" in ctx.extra["post_scan_errors"]

    def test_js_clone_hit_with_rule_id(self, make_proj_ctx):
        """js 克隆命中携带 JS-CLONE 规则 id（花括号行保留结构）。"""
        body = (
            "  const out = [];\n"
            "  for (const it of items) {\n"
            "    if (it > 0) {\n"
            "      out.push(it);\n"
            "    }\n"
            "  }\n"
            "  return out;\n"
        )

        def func(name: str) -> str:
            return f"function {name}(items) {{\n{body}}}\n"

        ctx = make_proj_ctx({"a.js": func("one") + "\n" + func("two")})
        issues = crossfile.run(ctx)
        assert len(issues) == 1
        assert "rule:JS-CLONE" in issues[0].evidence


# ------------------------------------------------- W20 追加：Type-2 二级匹配


def _func_sig(name: str, param: str, body: str) -> str:
    """带自定义参数名的 def（Type-2 用例需要参数名也可不同）。"""
    return f"def {name}({param}):\n{body}\n"


# 8 行函数体：alpha/beta 仅标识符改名 + 常量不同，一级归一化不同、二级归一化相同
_T2_BODY_A = """    total = 0
    for item in items:
        if item > 10:
            total += item
            audit(item)
            trace(item)
        extra = item * 2
    return total"""

_T2_BODY_B = """    acc = 0
    for row in rows:
        if row > 99:
            acc += row
            audit(row)
            trace(row)
        extra = row * 7
    return acc"""

# 6 行函数体（def 行计入后共 7 行 < 8 行阈值）：Type-2 不应命中
_T2_SHORT_A = """    total = 0
    for item in items:
        if item > 10:
            total += item
        extra = item * 2
    return total"""

_T2_SHORT_B = """    acc = 0
    for row in rows:
        if row > 99:
            acc += row
        extra = row * 7
    return acc"""


class TestCrossfileType2:
    """W20 Type-2 结构相似通道：改名 + 常量不同，归一化标识符后命中。"""

    def test_type2_renamed_clone_hit_same_file(self, make_proj_ctx):
        """同文件改名克隆：1 条 Type-2，low/0.5/type:2，挂在第二处函数。"""
        text = (
            _func_sig("alpha", "items", _T2_BODY_A)
            + "\n\n"
            + _func_sig("beta", "rows", _T2_BODY_B)
        )
        ctx = make_proj_ctx({"m.py": text})
        issues = crossfile.run(ctx)
        assert len(issues) == 1
        issue = issues[0]
        assert issue.severity == Severity.LOW
        assert issue.confidence == pytest.approx(0.5)
        assert issue.category == Category.STYLE and issue.source == IssueSource.RULE
        assert "rule:PY-CLONE" in issue.evidence and "type:2" in issue.evidence
        # alpha：def 第 1 行 + 8 行体 = 1~9；beta：def 第 12 行 + 8 行体 = 12~20
        assert (issue.line_start, issue.line_end) == (12, 20)
        assert "m.py 第 1~9 行" in issue.title
        assert "结构相似" in issue.title and "请人工确认" in issue.title
        assert "Type-2" in issue.description

    def test_type2_renamed_clone_hit_cross_file(self, make_proj_ctx):
        """跨文件改名克隆：命中挂在 b.py，evidence 带 type:2。"""
        ctx = make_proj_ctx(
            {
                "a.py": _func_sig("alpha", "items", _T2_BODY_A),
                "b.py": _func_sig("beta", "rows", _T2_BODY_B),
            }
        )
        issues = crossfile.run(ctx)
        assert len(issues) == 1
        issue = issues[0]
        assert issue.file == "b.py"
        assert (issue.line_start, issue.line_end) == (1, 9)
        assert "a.py 第 1~9 行" in issue.title
        assert issue.severity == Severity.LOW
        assert "type:2" in issue.evidence and "rule:PY-CLONE" in issue.evidence

    def test_type1_takes_priority_over_type2(self, make_proj_ctx):
        """Type-1 优先：逐字重复片段只报 Type-1（medium/0.7、无 type:2），不重复报 Type-2。"""
        text = _func("alpha", _BODY_7) + "\n\n" + _func("beta", _BODY_7)
        ctx = make_proj_ctx({"m.py": text})
        issues = crossfile.run(ctx)
        assert len(issues) == 1
        issue = issues[0]
        assert issue.severity == Severity.MEDIUM
        assert issue.confidence == pytest.approx(0.7)
        assert all(not ev.startswith("type:2") for ev in issue.evidence)

    def test_type2_below_threshold_not_reported(self, make_proj_ctx):
        """8 行以下（def 行计入后 7 行）不报 Type-2，也不产生 Type-1 命中。"""
        text = (
            _func_sig("alpha", "items", _T2_SHORT_A)
            + "\n\n"
            + _func_sig("beta", "rows", _T2_SHORT_B)
        )
        ctx = make_proj_ctx({"m.py": text})
        assert crossfile.run(ctx) == []

    def test_type2_same_function_self_similarity_not_reported(self, make_proj_ctx):
        """同函数体内部自相似（两段改名同构循环）不报 Type-2。"""
        text = (
            "def mega(data):\n"
            "    acc = 0\n"
            "    for value in data:\n"
            "        if value > 3:\n"
            "            acc += value\n"
            "            note(value)\n"
            "            probe(value)\n"
            "            mark(value)\n"
            "            held = value\n"
            "    other = 0\n"
            "    for item in data:\n"
            "        if item > 7:\n"
            "            other += item\n"
            "            note(item)\n"
            "            probe(item)\n"
            "            mark(item)\n"
            "            held = item\n"
            "    return acc + other\n"
        )
        ctx = make_proj_ctx({"m.py": text})
        assert crossfile.run(ctx) == []

    def test_clean_excerpt_zero_hit(self, make_proj_ctx):
        """clean 语料摘录（结构各异的安全函数）Type-1/Type-2 全部 0 命中。"""
        clean_py = (
            "import logging\n"
            "\n"
            "logger = logging.getLogger(__name__)\n"
            "\n"
            "\n"
            "def summarize(orders):\n"
            "    known = {o.order_id for o in orders}\n"
            "    total = sum(o.amount for o in orders if o.order_id in known)\n"
            '    logger.info("summarized orders total=%s count=%s", total, len(orders))\n'
            '    return {"total": total, "count": len(orders)}\n'
            "\n"
            "\n"
            "def run_sync(command):\n"
            '    proc = subprocess.run(command, capture_output=True, text=True, check=True)\n'
            "    return proc.stdout.strip()\n"
            "\n"
            "\n"
            "def fetch_remote(url):\n"
            "    with urlopen(url, timeout=10) as resp:\n"
            "        return resp.read()\n"
            "\n"
            "\n"
            "def validate_amount(amount):\n"
            "    if amount <= 0:\n"
            '        logger.warning("invalid amount: %s", amount)\n'
            "        return False\n"
            "    return True\n"
        )
        clean_js = (
            "export function pick(items) {\n"
            "  const seen = new Set();\n"
            "  const out = [];\n"
            "  for (const it of items) {\n"
            "    if (seen.has(it)) {\n"
            "      continue;\n"
            "    }\n"
            "    seen.add(it);\n"
            "    out.push(it);\n"
            "  }\n"
            "  return out;\n"
            "}\n"
        )
        ctx = make_proj_ctx({"util.py": clean_py, "util.js": clean_js})
        assert crossfile.run(ctx) == []

    def test_type2_env_override_min_lines(self, make_proj_ctx, monkeypatch):
        """CODEAUDIT_T2_MIN_LINES 可调阈值：12 时不报（9 行块 < 12），6 时报。"""
        files = {"m.py": _func_sig("alpha", "items", _T2_BODY_A) + "\n\n" + _func_sig("beta", "rows", _T2_BODY_B)}
        monkeypatch.setenv("CODEAUDIT_T2_MIN_LINES", "12")
        assert crossfile.run(make_proj_ctx(files)) == []
        monkeypatch.setenv("CODEAUDIT_T2_MIN_LINES", "6")
        issues = crossfile.run(make_proj_ctx(files))
        assert len(issues) == 1
        assert "type:2" in issues[0].evidence

    def test_type2_js_clone_hit(self, make_proj_ctx):
        """js 改名克隆：二级归一化后命中，携带 JS-CLONE + type:2。"""
        first = (
            "function collectA(list) {\n"
            "  const out = [];\n"
            "  for (const it of list) {\n"
            "    if (it > 1) {\n"
            "      out.push(it);\n"
            "      tap(it);\n"
            "    }\n"
            "  }\n"
            "  return out;\n"
            "}\n"
        )
        second = (
            "function collectB(list) {\n"
            "  const bag = [];\n"
            "  for (const el of list) {\n"
            "    if (el > 55) {\n"
            "      bag.push(el);\n"
            "      tap(el);\n"
            "    }\n"
            "  }\n"
            "  return bag;\n"
            "}\n"
        )
        ctx = make_proj_ctx({"a.js": first + second})
        issues = crossfile.run(ctx)
        assert len(issues) == 1
        issue = issues[0]
        assert issue.severity == Severity.LOW
        assert issue.confidence == pytest.approx(0.5)
        assert "rule:JS-CLONE" in issue.evidence and "type:2" in issue.evidence
        # collectA 1~10 行，collectB 11~20 行
        assert (issue.line_start, issue.line_end) == (11, 20)
