"""tests/integration 公共设施（W5-A1，R2 审查的公共设施要求，见 docs/10 §3/§5）。

本目录只做「联调」：CLI / API / 真实七阶段流水线端到端，全程零网络零真实 LLM：

- autouse fixture 消毒环境变量（GLM_API_KEY / GLM_BASE_URL / GLM_MODEL delenv），
  子进程（CLI 联调用 sys.executable）继承消毒后的 os.environ，同样零网络；
- 所有用例显式 --work-root / --out（或 work_root / out_dir）指向 tmp_path；
  test_hygiene.py 在套件收尾断言仓库根不新增 .codeaudit 目录；
- make_git_project 公共工厂：git init --separate-git-dir + user config +
  core.autocrlf=false + LF 写盘（Windows 防 .git 目录句柄占用与 CRLF 漂移）；
- scripted_llm fixture：参考 demo/run_demo.py 的 DemoScriptedLLM（按 system
  prompt 关键词分发：修复工程师→SQL 参数化 diff、测试工程师→pytest 代码、
  其余空响应），配合 run_with_scripted_llm 替换 audit.orchestrator.pipeline._make_llm。

规约（docs/10 §5 / R2）：禁固定 sleep（一律事件/条件等待）；禁测量型时序断言；
沙箱相关用例沿用实现默认超时（本套件不额外压时限）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

# 仓库根（cli.py / audit 包所在目录）
ROOT = Path(__file__).resolve().parents[2]

# 必须消毒的 LLM 环境变量：保证 llm_available=False（或仅注入脚本化客户端）
LLM_ENV_KEYS = ("GLM_API_KEY", "GLM_BASE_URL", "GLM_MODEL")


@pytest.fixture(autouse=True)
def sanitize_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """消毒 LLM 环境变量：任何用例都不可能触网或用到真实 GLM 凭据。"""
    for key in LLM_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def pytest_configure(config: pytest.Config) -> None:
    """注册本目录使用的标记（marker 必须先注册，避免未知标记告警）。"""
    config.addinivalue_line(
        "markers", "hygiene_last: 卫生检查标记，被移到整个会话最后执行（套件收尾断言）"
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """把带 hygiene_last 标记的用例挪到会话最后：保证「套件运行后」语义成立。"""
    last = [item for item in items if item.get_closest_marker("hygiene_last")]
    if last:
        items[:] = [item for item in items if not item.get_closest_marker("hygiene_last")] + last


# ---------------------------------------------------------------- 项目工厂


@pytest.fixture
def make_project(tmp_path: Path) -> Callable[[dict[str, str], str], Path]:
    """普通（非 git）项目工厂：make_project({"rel/path.py": "源码"}, name="proj")。

    一律 UTF-8 + LF 写盘，路径分隔符由调用方以 posix 风格给出。
    """

    counter = {"n": 0}

    def _make(files: dict[str, str], name: str = "proj") -> Path:
        counter["n"] += 1
        root = tmp_path / f"{name}_{counter['n']}"
        root.mkdir(parents=True)
        for rel, content in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="\n")
        return root

    return _make


@pytest.fixture
def make_git_project(tmp_path: Path) -> Callable[..., Path]:
    """git 项目工厂（R2 公共设施要求）。

    - ``git init --separate-git-dir``：真实 git 目录放在项目树之外（tmp_path 下），
      项目内只留 .git 指针文件——Windows 上刚建的 .git 目录可能被安全软件/句柄
      短暂占用，ingest 的 copytree 偶发 PermissionError，指针文件复制安全；
    - 本仓库级 user.name/email + core.autocrlf=false，内容一律 LF 写盘；
    - 默认完成首次提交（diff_ref="HEAD" 立即可用），``commit=False`` 可关闭。

    用法：``root = make_git_project({"a.py": ...}, subdir="services/app")``；
    返回仓库根（subdir 的父目录），审计靶子目录为 ``root / subdir``。
    """

    counter = {"n": 0}

    def _git(root: Path, *args: str) -> None:
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)

    def _make(
        files: dict[str, str],
        *,
        subdir: str | None = None,
        commit: bool = True,
        name: str = "gitproj",
    ) -> Path:
        counter["n"] += 1
        root = tmp_path / f"{name}_{counter['n']}"
        root.mkdir(parents=True)
        git_dir = tmp_path / f"{name}_{counter['n']}.gitdir"
        git_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "init", "--separate-git-dir", str(git_dir), str(root)],
            check=True,
            capture_output=True,
        )
        _git(root, "config", "user.email", "audit@example.com")
        _git(root, "config", "user.name", "auditor")
        _git(root, "config", "core.autocrlf", "false")
        target = root / subdir if subdir else root
        for rel, content in files.items():
            path = target / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="\n")
        if commit:
            _git(root, "add", ".")
            _git(root, "commit", "-qm", "init")
        return root

    return _make


# ---------------------------------------------------------------- 脚本化 LLM（复刻 demo/run_demo.py 思路，自包含）

# 靶项目与 demo/mini_app 同源：store.py 埋一个 SQL 拼接注入（critical），
# tests/test_store.py 为现有测试（修复前后都应通过），其余文件保持干净。
MINI_APP_FILES: dict[str, str] = {
    "store.py": (
        '"""用户存储：演示项目的缺陷靶点（已知 SQL 拼接注入，critical）。"""\n'
        "\n"
        "\n"
        "def find_user(conn, username):\n"
        '    """按用户名查询用户记录。"""\n'
        '    query = "SELECT * FROM users WHERE name = \'" + username + "\'"\n'
        "    return conn.execute(query).fetchall()\n"
    ),
    "textutil.py": (
        '"""文本工具：mini_app 的辅助模块（保持干净，无待修复缺陷）。"""\n'
        "\n"
        "\n"
        'def slugify(text, separator="-"):\n'
        '    """把任意字符串转成 URL 友好的 slug。"""\n'
        '    cleaned = "".join(ch.lower() if ch.isalnum() else separator for ch in text.strip())\n'
        "    while separator * 2 in cleaned:\n"
        "        cleaned = cleaned.replace(separator * 2, separator)\n"
        "    return cleaned.strip(separator)\n"
    ),
    "tests/test_store.py": (
        '"""现有测试：find_user 的正常路径（修复前后都应通过）。"""\n'
        "\n"
        "import sqlite3\n"
        "\n"
        "from store import find_user\n"
        "\n"
        "\n"
        "def _memory_conn():\n"
        '    conn = sqlite3.connect(":memory:")\n'
        "    conn.execute(\"CREATE TABLE users (name TEXT PRIMARY KEY, role TEXT)\")\n"
        "    conn.execute(\"INSERT INTO users VALUES ('alice', 'admin')\")\n"
        "    conn.commit()\n"
        "    return conn\n"
        "\n"
        "\n"
        "def test_find_user_returns_matching_row():\n"
        '    rows = find_user(_memory_conn(), "alice")\n'
        '    assert [tuple(r) for r in rows] == [("alice", "admin")]\n'
        "\n"
        "\n"
        "def test_find_user_unknown_name_returns_empty():\n"
        '    rows = find_user(_memory_conn(), "nobody")\n'
        "    assert rows == []\n"
    ),
}

# Fix Agent 脚本：与 MINI_APP_FILES["store.py"] 逐字节对齐的 unified diff（4→4 行）
SCRIPTED_FIX_DIFF = (
    "diff --git a/store.py b/store.py\n"
    "--- a/store.py\n"
    "+++ b/store.py\n"
    "@@ -4,4 +4,4 @@\n"
    " def find_user(conn, username):\n"
    '     """按用户名查询用户记录。"""\n'
    "-    query = \"SELECT * FROM users WHERE name = '\" + username + \"'\"\n"
    "-    return conn.execute(query).fetchall()\n"
    '+    query = "SELECT * FROM users WHERE name = ?"\n'
    "+    return conn.execute(query, (username,)).fetchall()\n"
)
SCRIPTED_FIX_RATIONALE = (
    "把用户名从 SQL 字符串拼接改为占位符 ? + 参数元组，由驱动层完成转义，"
    "彻底消除注入面；函数签名与返回值不变，现有调用方无需修改。"
)

# TestGen Agent 脚本：针对修复后 find_user 的回归用例（含注入载荷用例，真跑必须全绿）
SCRIPTED_TESTGEN_CODE = '''import sqlite3

from store import find_user


def _memory_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE users (name TEXT PRIMARY KEY, role TEXT)")
    conn.execute("INSERT INTO users VALUES ('alice', 'admin')")
    conn.execute("INSERT INTO users VALUES ('bob', 'viewer')")
    conn.commit()
    return conn


# kind: normal
def test_find_user_returns_matching_row():
    rows = find_user(_memory_conn(), "alice")
    assert [tuple(r) for r in rows] == [("alice", "admin")]


# kind: boundary
def test_find_user_unknown_name_returns_empty():
    assert find_user(_memory_conn(), "nobody") == []


# kind: error
def test_find_user_treats_injection_payload_as_literal():
    # 修复前该载荷会让 WHERE 恒真并返回全表；修复后按字面值匹配，必须为空
    rows = find_user(_memory_conn(), "x' OR '1'='1")
    assert rows == []
'''


class ScriptedLLM:
    """脚本化假 LLM（按 system prompt 关键词分发，不依赖调用顺序）。

    与 demo/run_demo.py 的 DemoScriptedLLM 同思路；为避免测试导入 demo 模块，
    此处独立实现，接口与 audit.llm.base.LLMClient 兼容（鸭子类型即可）。
    """

    def __init__(self) -> None:
        self.calls: dict[str, int] = {"fix": 0, "testgen": 0, "other": 0}

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        json_mode: bool = False,
        temperature: float = 0.2,
    ) -> Any:
        import json

        from audit.llm.base import LLMResponse

        system = next(
            (str(m.get("content", "")) for m in messages if m.get("role") == "system"), ""
        )
        if "修复工程师" in system:
            self.calls["fix"] += 1
            payload = {"diff": SCRIPTED_FIX_DIFF, "rationale": SCRIPTED_FIX_RATIONALE}
            return LLMResponse(content=json.dumps(payload, ensure_ascii=False), model="scripted")
        if "测试工程师" in system:
            self.calls["testgen"] += 1
            return LLMResponse(content=f"```python\n{SCRIPTED_TESTGEN_CODE}```", model="scripted")
        self.calls["other"] += 1
        return LLMResponse(content="", model="scripted")

    def usage_totals(self) -> dict[str, int]:
        return {"llm_calls": sum(self.calls.values()), "prompt_tokens": 0, "completion_tokens": 0}

    # A3 资源收口（R1-3）后流水线会对客户端调用 aclose；脚本化客户端无需清理
    async def aclose(self) -> None:
        return None


@pytest.fixture
def mini_app_files() -> dict[str, str]:
    """靶项目文件（与 demo/mini_app 同源）：store.py 一个 critical SQL 注入 + 现有 pytest 测试。"""
    return dict(MINI_APP_FILES)


@pytest.fixture
def scripted_llm() -> ScriptedLLM:
    return ScriptedLLM()


@pytest.fixture
def run_with_scripted_llm(scripted_llm: ScriptedLLM) -> Callable[[Any, Any], Any]:
    """返回 ``await run(config, emit)``：替换 pipeline._make_llm 为脚本化客户端执行审计。

    与 demo/run_demo.py 相同的装配缝隙；结束（含异常）后还原，不污染其他用例。
    """

    async def _run(config: Any, emit: Any) -> Any:
        import audit.orchestrator.pipeline as pipeline_module

        original = pipeline_module._make_llm
        pipeline_module._make_llm = lambda _config: (scripted_llm, None)  # noqa: E731
        try:
            return await pipeline_module.run_audit(config, emit)
        finally:
            pipeline_module._make_llm = original

    return _run


# ---------------------------------------------------------------- 事件收集与观察工具


@pytest.fixture
def events_collector() -> Any:
    """收集进度事件的 emitter：``collector.events`` 为事件列表。"""
    events: list[dict[str, Any]] = []

    async def emit(event: dict[str, Any]) -> None:
        events.append(dict(event))

    emit.events = events  # type: ignore[attr-defined]
    return emit


@pytest.fixture
def stage_order() -> Callable[[list[dict[str, Any]]], list[str]]:
    """从事件流提取阶段出现顺序（按首次出现去重）。"""

    def _order(events: list[dict[str, Any]]) -> list[str]:
        seen: list[str] = []
        for event in events:
            stage = event.get("stage")
            if stage and stage not in seen:
                seen.append(stage)
        return seen

    return _order


@pytest.fixture
def stage_messages() -> Callable[[list[dict[str, Any]], str], list[str]]:
    """取某阶段的全部事件文案。"""

    def _messages(events: list[dict[str, Any]], stage: str) -> list[str]:
        return [str(e.get("message", "")) for e in events if e.get("stage") == stage]

    return _messages


# ---------------------------------------------------------------- CLI 联调工具


@pytest.fixture
def run_cli_subprocess() -> Callable[..., subprocess.CompletedProcess[str]]:
    """以真实子进程运行 ``sys.executable cli.py <args>``（cwd=仓库根）。

    - 沿用 pytest 进程的 os.environ（autouse 消毒已生效，继承即零网络）；
    - PYTHONIOENCODING=utf-8 固化子进程输出编码（Windows 管道默认 GBK）；
    - 超时给到 2x 余量（demo_proj 全流程实测 ~3s，这里放宽到 180s）。
    """

    def _run(args: list[str], *, cwd: Path = ROOT, timeout: float = 180.0) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        return subprocess.run(
            [sys.executable, "cli.py", *args],
            cwd=str(cwd),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            check=False,
        )

    return _run


@pytest.fixture
def repo_root_snapshot() -> dict[str, Any]:
    """会话级快照：仓库根 .codeaudit 目录在套件开始前的存在性与任务表项清单。

    供卫生测试判断「套件是否新增审计工作区」——仓库根可能残留用户历史运行
    （如 dogfood 时未带 --work-root）产生的 .codeaudit，此时硬断言"目录不存在"
    会误报；改为断言套件运行后没有新增 audit_id 子目录。
    """
    codeaudit = ROOT / ".codeaudit"
    return {
        "existed": codeaudit.exists(),
        "entries": sorted(p.name for p in codeaudit.iterdir()) if codeaudit.is_dir() else [],
    }
