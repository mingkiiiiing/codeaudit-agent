"""audit/utils.py 单元测试（W15-F 卡F：测试盲区补齐）。

覆盖范围：全公共函数边界——
- extract_json：合法输入 / 代码围栏 / 前后缀噪音 / 非法输入 / 「首{到末}」截取策略（含现状局限）；
- issue_fingerprint：稳定性、四元组（file|行区间|类别|标题前60字）敏感性、对描述措辞与置信度不敏感；
- truncate / truncate_lines：边界长度与省略号语义；
- make_id / new_audit_id / now_iso / sha256_text / sha256_file / count_lines / guess_language。
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from audit.models import Category, Issue
from audit.utils import (
    count_lines,
    extract_json,
    guess_language,
    issue_fingerprint,
    make_id,
    new_audit_id,
    now_iso,
    sha256_file,
    sha256_text,
    truncate,
    truncate_lines,
)

# ---------------------------------------------------------------- guess_language


class TestGuessLanguage:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("a.py", "python"),
            ("A.PY", "python"),  # 扩展名大小写不敏感
            ("a.js", "javascript"),
            ("a.mjs", "javascript"),
            ("a.cjs", "javascript"),
            ("a.jsx", "javascript"),
            ("a.ts", "typescript"),
            ("a.tsx", "typescript"),
            ("a.go", None),  # 未登记扩展名
            ("a", None),  # 无扩展名
            ("a.PYTHON", None),  # 非精确后缀不误判
        ],
    )
    def test_by_suffix(self, name: str, expected: str | None):
        assert guess_language(name) == expected

    def test_accepts_path_object(self):
        assert guess_language(Path("x/y/main.py")) == "python"


# ---------------------------------------------------------------- sha256 / count_lines


class TestSha256:
    def test_sha256_text_known_digest(self):
        # 空串是 SHA-256 标准金标准值；非空用 hashlib 交叉验证 utf-8 编码路径
        assert sha256_text("") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        assert sha256_text("hello") == hashlib.sha256(b"hello").hexdigest()
        assert re.fullmatch(r"[0-9a-f]{64}", sha256_text("任意中文内容"))

    def test_sha256_text_stable_and_sensitive(self):
        assert sha256_text("abc") == sha256_text("abc")
        assert sha256_text("abc") != sha256_text("abd")

    def test_sha256_text_replaces_lone_surrogate(self):
        # errors="replace"：含代理逃逸字符的 str 无法按 utf-8 编码，但不抛异常——
        # encode 的 replace 语义是替换为 "?"（注意与 decode 的 U+FFFD 不同）
        assert sha256_text("a\ud800b") == sha256_text("a?b")
        assert sha256_text("a\ud800b") == hashlib.sha256("a?b".encode("utf-8")).hexdigest()

    def test_sha256_file_matches_text_hash(self, tmp_path):
        f1 = tmp_path / "a.txt"
        f1.write_bytes(b"hello world")
        f2 = tmp_path / "b.txt"
        f2.write_bytes(b"hello world!")
        assert sha256_file(f1) == sha256_text("hello world")
        assert sha256_file(f1) != sha256_file(f2)
        assert sha256_file(str(f1)) == sha256_file(f1)  # str 与 Path 等价


class TestCountLines:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("", 0),
            ("a", 1),
            ("a\n", 1),  # 末尾换行不计新行
            ("a\nb", 2),
            ("a\nb\n", 2),
            ("\n", 1),
            ("\n\n", 2),
            ("a\r\nb", 2),  # \r\n 按一个换行计
        ],
    )
    def test_count(self, text: str, expected: int):
        assert count_lines(text) == expected


# ---------------------------------------------------------------- truncate / truncate_lines


class TestTruncate:
    def test_short_text_unchanged(self):
        assert truncate("abc", 100) == "abc"

    def test_exact_boundary_unchanged(self):
        text = "a" * 2000
        assert truncate(text) == text  # len == max_chars 不截断
        assert truncate("ab", 2) == "ab"

    def test_over_boundary_appends_ellipsis_with_total(self):
        out = truncate("abcdef", 4)
        assert out == "abcdef"[:4] + "\n...[截断，共 6 字符]"

    def test_custom_max_chars(self):
        assert truncate("hello", 3) == "hel\n...[截断，共 5 字符]"

    def test_zero_max_chars_keeps_prefix_empty(self):
        # 边界：max_chars=0 时正文为空、仅剩省略号说明（固化现状语义）
        assert truncate("abc", 0) == "\n...[截断，共 3 字符]"

    def test_default_max_chars_is_2000(self):
        assert truncate("a" * 2001) == "a" * 2000 + "\n...[截断，共 2001 字符]"


class TestTruncateLines:
    def test_short_list_unchanged(self):
        lines = ["a", "b"]
        assert truncate_lines(lines, 10) == lines

    def test_exact_boundary_unchanged(self):
        lines = ["a", "b", "c"]
        assert truncate_lines(lines, 3) == lines

    def test_over_boundary_appends_ellipsis_with_total(self):
        lines = ["l0", "l1", "l2", "l3", "l4"]
        assert truncate_lines(lines, 3) == ["l0", "l1", "l2", "...[截断，共 5 行]"]

    def test_default_max_lines_is_200(self):
        lines = [str(i) for i in range(201)]
        out = truncate_lines(lines)
        assert len(out) == 201
        assert out[200] == "...[截断，共 201 行]"


# ---------------------------------------------------------------- id 生成


class TestMakeId:
    @pytest.mark.parametrize(
        ("prefix", "n", "expected"),
        [
            ("ISS", 42, "ISS-0042"),
            ("ISS", 0, "ISS-0000"),
            ("PATCH", 7, "PATCH-0007"),
            ("ISS", 9999, "ISS-9999"),
            ("ISS", 12345, "ISS-12345"),  # 超 4 位不再补零、也不截断
        ],
    )
    def test_format(self, prefix: str, n: int, expected: str):
        assert make_id(prefix, n) == expected


class TestNewAuditId:
    def test_format_is_12_lowercase_hex(self):
        for aid in (new_audit_id() for _ in range(20)):
            assert re.fullmatch(r"[0-9a-f]{12}", aid)

    def test_uniqueness(self):
        assert len({new_audit_id() for _ in range(500)}) == 500


class TestNowIso:
    def test_utc_and_second_precision(self):
        iso = now_iso()
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00", iso)
        parsed = datetime.fromisoformat(iso)
        assert parsed.utcoffset() is not None
        assert parsed.utcoffset().total_seconds() == 0  # 必须是 UTC


# ---------------------------------------------------------------- issue_fingerprint（契约 v1.4）


def _make_issue(**overrides) -> Issue:
    base = dict(
        file="app/services/orders.py",
        line_start=10,
        line_end=20,
        title="可变默认参数",
    )
    base.update(overrides)
    return Issue(**base)


class TestIssueFingerprint:
    def test_format_is_sha1_hex(self):
        assert re.fullmatch(r"[0-9a-f]{40}", issue_fingerprint(_make_issue()))

    def test_stable_across_calls_and_equal_issues(self):
        assert issue_fingerprint(_make_issue()) == issue_fingerprint(_make_issue())

    def test_enum_and_plain_str_category_equivalent(self):
        # 类别可传 Category 枚举或等值字符串，指纹必须一致（契约：取 value）
        enum_issue = _make_issue(category=Category.BUG)
        ns = SimpleNamespace(
            file="app/services/orders.py", line_start=10, line_end=20, category="bug", title="可变默认参数"
        )
        assert issue_fingerprint(enum_issue) == issue_fingerprint(ns)

    @pytest.mark.parametrize(
        "overrides",
        [
            {"file": "app/services/users.py"},  # 文件变
            {"line_start": 11},  # 起行变
            {"line_end": 21},  # 止行变
            {"category": Category.SECURITY},  # 类别变
            {"title": "不可变默认参数"},  # 标题变
        ],
    )
    def test_sensitive_to_four_tuple_fields(self, overrides: dict):
        assert issue_fingerprint(_make_issue(**overrides)) != issue_fingerprint(_make_issue())

    @pytest.mark.parametrize(
        "overrides",
        [
            {"description": "完全不同的措辞"},
            {"confidence": 0.99},
            {"severity": "critical"},
            {"evidence": ["line 10"]},
            {"suggestion": "改为 None 默认值"},
        ],
    )
    def test_insensitive_to_non_key_fields(self, overrides: dict):
        # 与描述措辞/置信度/严重度无关——这是基线跨次审计匹配的根基
        assert issue_fingerprint(_make_issue(**overrides)) == issue_fingerprint(_make_issue())

    def test_title_truncated_at_60_chars(self):
        # 契约明文「标题前 60 字」：60 字之后的变化不影响指纹
        title_a = "A" * 60 + "尾部差异甲"
        title_b = "A" * 60 + "尾部差异乙"
        assert issue_fingerprint(_make_issue(title=title_a)) == issue_fingerprint(_make_issue(title=title_b))
        assert issue_fingerprint(_make_issue(title="A" * 59 + "甲")) != issue_fingerprint(
            _make_issue(title="A" * 59 + "乙")
        )

    def test_none_title_treated_as_empty(self):
        # issue.title or ''：标题为 None 时与空串同指纹
        ns_none = SimpleNamespace(file="a.py", line_start=1, line_end=2, category="bug", title=None)
        ns_empty = SimpleNamespace(file="a.py", line_start=1, line_end=2, category="bug", title="")
        assert issue_fingerprint(ns_none) == issue_fingerprint(ns_empty)


# ---------------------------------------------------------------- extract_json


class TestExtractJson:
    def test_plain_object(self):
        assert extract_json('{"a": 1, "b": [2, 3]}') == {"a": 1, "b": [2, 3]}

    def test_plain_array(self):
        assert extract_json("[1, 2, 3]") == [1, 2, 3]

    def test_multiline_nested_object(self):
        text = '{\n  "a": {"b": [1, 2]},\n  "c": "text with } brace"\n}'
        # 字符串字面量内的 } 不会破坏解析（strip 候选先于截取候选）
        assert extract_json(text) == {"a": {"b": [1, 2]}, "c": "text with } brace"}

    def test_fenced_json_block(self):
        text = "前言\n```json\n{\"verdict\": \"confirmed\"}\n```\n后记"
        assert extract_json(text) == {"verdict": "confirmed"}

    def test_fenced_without_json_tag(self):
        text = "```\n[true, null]\n```"
        assert extract_json(text) == [True, None]

    def test_fenced_wins_over_trailing_braces(self):
        # 围栏候选优先于「首{到末}」截取：围栏外另有 JSON 时取围栏内
        text = 'result\n```json\n{"from": "fence"}\n```\nalso {"other": 1}'
        assert extract_json(text) == {"from": "fence"}

    def test_prefix_noise_only(self):
        assert extract_json('这是结果：{"a": 1}') == {"a": 1}

    def test_suffix_noise_only(self):
        assert extract_json('{"a": 1} 以上。') == {"a": 1}

    def test_noise_on_both_sides(self):
        assert extract_json('好的，结果如下 {"ok": true} 请查收') == {"ok": True}

    def test_leading_trailing_whitespace(self):
        assert extract_json('  \n\t{"a": 1}\n  ') == {"a": 1}

    def test_none_raises_value_error(self):
        with pytest.raises(ValueError, match="empty response"):
            extract_json(None)

    @pytest.mark.parametrize("text", ["", "   ", "完全不是 JSON", "123 abc"])
    def test_unparsable_raises_value_error(self, text: str):
        with pytest.raises(ValueError, match="no parsable JSON"):
            extract_json(text)

    def test_error_message_contains_truncated_snippet(self):
        junk = "x" * 500
        with pytest.raises(ValueError) as ei:
            extract_json(junk)
        msg = str(ei.value)
        assert "no parsable JSON" in msg
        assert "x" * 200 in msg  # 错误信息里截到 200 字符
        assert "x" * 201 not in msg

    def test_braces_in_noise_is_a_known_limitation(self):
        # W15-F 现状固化（已知局限，非背书）：噪音自带花括号时「首{到末}」整段截取
        # 会把噪音并进候选导致解析失败——更稳策略是逐个候选对象扫描。
        # 若生产侧修复此处，本测试应同步更新为断言返回 {"a": 1}。已记录上报。
        with pytest.raises(ValueError, match="no parsable JSON"):
            extract_json('note {noise} {"a": 1}')
