"""W30-C 注释行掩码预检集中测试（四语言 SECRET 规则家族）。

背景：四语言 HardcodedSecretRule 均在 raw 行上匹配 _ASSIGN_RE 提取声明与
字符串值，块注释中间行 / docstring（三引号）中间行 / 反引号原生串中间行的
假声明形态会被当真实赋值命中（家族级误报）。

本卡修复：各语言 SECRET 规则 check() 内新增「注释行掩码预检」——raw 行命中
声明形态后，将同正则再匹配该行 masked 版本（掩码扫描器把字符串内容与注释
置为空格、保留引号定界符位置）：masked 行不再命中 ⇒ 赋值形态只存在于注释/
字符串内容中，跳过不报；masked 行仍命中 ⇒ 照原逻辑继续（raw 上取值、熵闸门
等判定口径零变化）。四语言各自持有一份独立拷贝（家族惯例，不复用基类）。

用例分组：每语言 ≥3（注释内假声明不报 / 同文件真实赋值仍报=真阳性不误杀 /
行尾注释跟真实赋值仍报），外加「测试夹具假密钥（真实赋值行）仍报」的家族
回归。文中密钥均为测试用假密钥，非真实凭据。
"""

from __future__ import annotations

from audit.detect.base import Rule, RuleContext
from audit.detect.rules.cpp import CppHardcodedSecretRule
from audit.detect.rules.go import GoHardcodedSecretRule
from audit.detect.rules.java import JavaHardcodedSecretRule
from audit.detect.rules.python import HardcodedSecretRule

# 已知假密钥（仅测试用，非真实凭据）
FAKE_SK = "sk-aX9kQ2vL8mN4pR7sT5uW3yZ0"
FAKE_HIGH_ENTROPY = "aX9kQ2vL8mN4pR7sT5uW3yZ1"
FAKE_PASSWORD = "mysupersecretkey123456"


def _ctx(source: str, rel_path: str, language: str) -> RuleContext:
    return RuleContext(rel_path=rel_path, language=language, source=source, lines=source.splitlines())


def _lines(rule: Rule, ctx: RuleContext) -> list[int]:
    return [h.line_start for h in rule.check(ctx)]


# ---------------------------------------------------------------- python


class TestPySecretCommentMask:
    rule = HardcodedSecretRule()

    def test_docstring_fake_assign_clean(self):
        # docstring（三引号）中间行的假声明：masked 后内容置空 ⇒ 不报
        src = (
            'HELP = """\n'
            f'API_KEY = "{FAKE_HIGH_ENTROPY}"\n'
            '"""\n'
        )
        assert _lines(self.rule, _ctx(src, "m.py", "python")) == []

    def test_real_assign_after_docstring_still_hits(self):
        # 同文件真实赋值（docstring 之后）仍报：真阳性不误杀
        src = (
            'HELP = """\n'
            f'API_KEY = "{FAKE_HIGH_ENTROPY}"\n'
            '"""\n'
            f'API_KEY = "{FAKE_HIGH_ENTROPY}"\n'
        )
        assert _lines(self.rule, _ctx(src, "m.py", "python")) == [4]

    def test_trailing_comment_on_real_assign_still_hits(self):
        # 真实赋值 + 行尾注释：注释被掩码但赋值形态仍在 ⇒ 照原逻辑报
        src = f'API_KEY = "{FAKE_HIGH_ENTROPY}"  # 仅供本地联调\n'
        assert _lines(self.rule, _ctx(src, "m.py", "python")) == [1]

    def test_full_line_comment_fake_assign_clean(self):
        # 整行注释内的假声明（定界符挡住 name，raw 本就不命中）：不报
        src = f'# API_KEY = "{FAKE_HIGH_ENTROPY}"\n'
        assert _lines(self.rule, _ctx(src, "m.py", "python")) == []


# ---------------------------------------------------------------- java


class TestJavaSecretCommentMask:
    rule = JavaHardcodedSecretRule()

    def test_block_comment_fake_assign_clean(self):
        # 块注释中间行的假声明：masked 后整行置空 ⇒ 不报
        src = (
            '/*\n'
            f'String apiKey = "{FAKE_HIGH_ENTROPY}";\n'
            '*/\n'
        )
        assert _lines(self.rule, _ctx(src, "Foo.java", "java")) == []

    def test_text_block_fake_assign_clean(self):
        # 文本块（\"\"\"）中间行的假声明：masked 后整行置空 ⇒ 不报
        src = (
            'String HELP = """\n'
            f'String dbPassword = "{FAKE_PASSWORD}";\n'
            '""";\n'
        )
        assert _lines(self.rule, _ctx(src, "Foo.java", "java")) == []

    def test_real_assign_in_same_file_still_hits(self):
        # 同文件真实赋值仍报：真阳性不误杀
        src = (
            '/*\n'
            f'String apiKey = "{FAKE_HIGH_ENTROPY}";\n'
            '*/\n'
            'String dbPassword = "mysupersecretkey123456";\n'
        )
        assert _lines(self.rule, _ctx(src, "Foo.java", "java")) == [4]

    def test_trailing_comment_on_real_assign_still_hits(self):
        # 真实赋值 + 行尾注释：注释被掩码但赋值形态仍在 ⇒ 照原逻辑报
        src = f'String apiKey = "{FAKE_HIGH_ENTROPY}"; // 仅供本地联调\n'
        assert _lines(self.rule, _ctx(src, "Foo.java", "java")) == [1]


# ---------------------------------------------------------------- go


class TestGoSecretCommentMask:
    rule = GoHardcodedSecretRule()

    def test_block_comment_fake_assign_clean(self):
        # 块注释中间行的假声明：masked 后整行置空 ⇒ 不报
        src = (
            '/*\n'
            f'const apiKey = "{FAKE_HIGH_ENTROPY}"\n'
            '*/\n'
        )
        assert _lines(self.rule, _ctx(src, "foo.go", "go")) == []

    def test_raw_string_fake_assign_clean(self):
        # 反引号原生串中间行的假声明：masked 后整行置空 ⇒ 不报
        src = (
            'var help = `\n'
            f'const dbPwd = "{FAKE_PASSWORD}"\n'
            '`\n'
        )
        assert _lines(self.rule, _ctx(src, "foo.go", "go")) == []

    def test_backtick_real_assign_still_hits(self):
        # 反引号原生串真实赋值（开定界符保留）仍报：真阳性不误杀
        src = f'const dbPwd = `{FAKE_PASSWORD}`\n'
        assert _lines(self.rule, _ctx(src, "foo.go", "go")) == [1]

    def test_real_assign_in_same_file_still_hits(self):
        # 同文件解释型字符串真实赋值 + 块注释中间行假声明共存：只报真实赋值
        src = (
            '/*\n'
            f'const apiKey = "{FAKE_HIGH_ENTROPY}"\n'
            '*/\n'
            f'const apiKey = "{FAKE_HIGH_ENTROPY}"\n'
        )
        assert _lines(self.rule, _ctx(src, "foo.go", "go")) == [4]

    def test_trailing_comment_on_real_assign_still_hits(self):
        # 真实赋值 + 行尾注释：注释被掩码但赋值形态仍在 ⇒ 照原逻辑报
        src = f'const apiKey = "{FAKE_HIGH_ENTROPY}" // 仅供本地联调\n'
        assert _lines(self.rule, _ctx(src, "foo.go", "go")) == [1]


# ---------------------------------------------------------------- cpp


class TestCppSecretCommentMask:
    rule = CppHardcodedSecretRule()

    def test_block_comment_fake_macro_clean(self):
        # 块注释中间行的假 #define：masked 后整行置空 ⇒ 不报
        src = (
            '/*\n'
            f'#define API_KEY "{FAKE_SK}"\n'
            '*/\n'
        )
        assert _lines(self.rule, _ctx(src, "foo.cpp", "cpp")) == []

    def test_block_comment_fake_assign_clean(self):
        # 块注释中间行的假 const 赋值：masked 后整行置空 ⇒ 不报
        src = (
            '/*\n'
            f'const std::string kApiKey = "{FAKE_HIGH_ENTROPY}";\n'
            '*/\n'
        )
        assert _lines(self.rule, _ctx(src, "foo.cpp", "cpp")) == []

    def test_real_assign_in_same_file_still_hits(self):
        # 同文件真实赋值仍报：真阳性不误杀
        src = (
            '/*\n'
            f'#define API_KEY "{FAKE_SK}"\n'
            '*/\n'
            f'const std::string kApiKey = "{FAKE_HIGH_ENTROPY}";\n'
        )
        assert _lines(self.rule, _ctx(src, "foo.cpp", "cpp")) == [4]

    def test_trailing_comment_on_real_assign_still_hits(self):
        # 真实赋值 + 行尾注释：注释被掩码但赋值形态仍在 ⇒ 照原逻辑报
        src = f'const std::string kApiKey = "{FAKE_HIGH_ENTROPY}"; // 仅供本地联调\n'
        assert _lines(self.rule, _ctx(src, "foo.cpp", "cpp")) == [1]


# ---------------------------------------------------------------- 家族回归


class TestSecretFixtureRegression:
    """家族回归：测试夹具假密钥写在真实赋值行上，四语言均必须仍报。"""

    def test_py_fixture_fake_key_still_hits(self):
        rule = HardcodedSecretRule()
        src = f'API_KEY = "{FAKE_SK}"\n'
        assert _lines(rule, _ctx(src, "m.py", "python")) == [1]

    def test_java_fixture_fake_key_still_hits(self):
        rule = JavaHardcodedSecretRule()
        src = f'String apiKey = "{FAKE_SK}";\n'
        assert _lines(rule, _ctx(src, "Foo.java", "java")) == [1]

    def test_go_fixture_fake_key_still_hits(self):
        rule = GoHardcodedSecretRule()
        src = f'const apiKey = "{FAKE_SK}"\n'
        assert _lines(rule, _ctx(src, "foo.go", "go")) == [1]

    def test_cpp_fixture_fake_key_still_hits(self):
        rule = CppHardcodedSecretRule()
        src = f'#define API_KEY "{FAKE_SK}"\n'
        assert _lines(rule, _ctx(src, "foo.cpp", "cpp")) == [1]
