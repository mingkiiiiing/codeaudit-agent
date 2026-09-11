"""金标扩充两件套（docs/04 §2.1）：git 历史回溯挖矿 + 规则缺陷注入。

- :func:`mine_git_history`：对本地 git 仓库筛"修复类"commit（消息含
  fix/bug/crash/error/patch，大小写不敏感；含 test/chore/docs 的跳过），
  用 ``git show`` 的 unified diff 反推金标——**行区间记录在修复前（父提交）
  版本的行号坐标系**上（docs/04 §2.1"回退到引入前版本"的口径：对父提交
  版本跑审计即可用这些金标对账）。纯新增文件/删除文件不产生金标。
- :func:`inject_defects`：把源项目复制到输出目录后，向随机选中的 .py 文件
  **只追加**独立函数形式的已知缺陷模板（不改动原文件任何已有内容），
  注入后必须 ``ast.parse`` 通过，失败的注入整体回退跳过。

subprocess 一律 list 参数调用 git，不带 shell=True；仓库非 git / git 不可用 /
无匹配 commit 时返回空列表，不抛异常。
"""

from __future__ import annotations

import ast
import random
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from bench.goldset import GoldenIssue, save_goldset

__all__ = [
    "DEFECT_TEMPLATES",
    "inject_defects",
    "mine_git_history",
]

# ---------------------------------------------------------------- 常量

FIX_KEYWORDS: tuple[str, ...] = ("fix", "bug", "crash", "error", "patch")
EXCLUDE_KEYWORDS: tuple[str, ...] = ("test", "chore", "docs")
SOURCE_EXTS: frozenset[str] = frozenset({".py", ".js", ".ts"})

# 扫描 commit 的候选池上限（先取池再筛修复类，避免超大仓库全量遍历）
_LOG_POOL = 1000

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@")


# ---------------------------------------------------------------- git 基建


def _run_git(repo: Path, args: list[str]) -> str:
    """在 repo 目录执行 git 子命令，返回 stdout 文本；失败抛 CalledProcessError。"""
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return proc.stdout


def _is_fix_commit(subject: str) -> bool:
    """修复类 commit 判定：消息含 fix/bug/crash/error/patch 且不含 test/chore/docs。"""
    lowered = subject.lower()
    if any(k in lowered for k in EXCLUDE_KEYWORDS):
        return False
    return any(k in lowered for k in FIX_KEYWORDS)


def _is_source_candidate(path: str) -> bool:
    """diff 文件是否值得生成金标：.py/.js/.ts 且非测试文件。"""
    from audit.detect.rules._python_common import is_test_file

    if Path(path).suffix.lower() not in SOURCE_EXTS:
        return False
    return not is_test_file(path)


def _guess_category(text: str) -> str:
    """按 commit 消息/diff 内容猜类别：security|cve|injection|secret → security，否则 bug。"""
    lowered = text.lower()
    if any(k in lowered for k in ("security", "cve", "injection", "secret")):
        return "security"
    return "bug"


# ---------------------------------------------------------------- diff 解析


def _parse_diff_hunks(diff_text: str) -> list[tuple[str, int, int]]:
    """解析 ``git show`` unified diff，返回 (文件相对路径, 行起点, 行终点) 列表。

    行区间为**修复前版本（old 侧）坐标**：
    - hunk 内含 ``-`` 行（修改/删除）→ 区间即这些被移除的缺陷行，精确到行；
    - 纯插入 hunk（无 ``-`` 行，典型如"补一个判空再走原逻辑"）→ 区间为
      插入点 p 及其后首个既有行（p, p+1)，把被守护的目标行纳入金标；
    上下文行不参与计数。新增文件（new file mode）与删除文件
    （+++ /dev/null）不产出区间。
    """
    out: list[tuple[str, int, int]] = []
    current_file: str | None = None
    file_is_new = False
    in_hunk = False
    consumed = 0  # 当前 hunk 内已消费的 old 侧行数
    removed: list[int] = []
    inserted: list[int] = []

    def _flush() -> None:
        if not current_file:
            return
        if removed:
            out.append((current_file, min(removed), max(removed)))
        elif inserted:
            point = min(inserted)
            out.append((current_file, point, point + 1))  # 纯插入：含被守护的既有行

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            _flush()
            current_file, file_is_new, in_hunk = None, False, False
            removed, inserted = [], []
            continue
        if line.startswith("new file mode"):
            file_is_new = True
            continue
        if line.startswith("+++ "):
            target = line[4:].strip()
            current_file = None if target == "/dev/null" else target[2:] if target.startswith("b/") else target
            continue
        if _HUNK_RE.match(line):
            _flush()
            old_start = int(line.split()[1][1:].split(",")[0])
            consumed = old_start - 1
            in_hunk = True
            removed, inserted = [], []
            continue
        if not in_hunk or current_file is None or file_is_new:
            continue
        if line.startswith("\\"):  # "\ No newline at end of file"
            continue
        if line.startswith("-"):
            consumed += 1
            removed.append(consumed)
        elif line.startswith("+"):
            inserted.append(consumed + 1)
        elif not line.startswith(("diff ", "@@")):
            # 上下文行（unified=0 时基本不出现）：推进 old 侧计数
            consumed += 1
    _flush()
    return out


def _candidate_commits(repo: Path) -> list[tuple[str, str]]:
    """返回 (sha, subject) 候选列表：git log 解析 + 修复类消息过滤（保持时间序）。"""
    text = _run_git(repo, ["log", f"--max-count={_LOG_POOL}", "--pretty=format:%H%x1f%s", "--name-only"])
    commits: list[tuple[str, str]] = []
    for line in text.splitlines():
        if "\x1f" in line:
            sha, subject = line.split("\x1f", 1)
            if _is_fix_commit(subject):
                commits.append((sha.strip(), subject.strip()))
    return commits


def mine_git_history(
    repo: Path,
    out_jsonl: Path,
    max_commits: int = 50,
    project: str | None = None,
) -> list[GoldenIssue]:
    """从本地 git 仓库的修复类 commit 中回溯挖掘金标并写 JSONL。

    - 仅处理修改类 diff 的 .py/.js/.ts 源码文件（排除测试文件）；
    - 类别按 commit 消息与 diff 内容猜测（security/cve/injection/secret →
      security，否则 bug），severity 一律 high（docs/04：修复 commit 视为高危）；
    - 仓库不是 git、git 不可用或无匹配 commit 时返回空列表，不抛异常。
    """
    repo = Path(repo)
    out_jsonl = Path(out_jsonl)
    project_name = project or repo.name
    try:
        candidates = _candidate_commits(repo)[: max(0, max_commits)]
    except (OSError, subprocess.CalledProcessError, FileNotFoundError):
        return []

    goldens: list[GoldenIssue] = []
    for sha, subject in candidates:
        try:
            diff_text = _run_git(repo, ["show", "--format=", "--patch", "--unified=0", sha])
        except (OSError, subprocess.CalledProcessError):
            continue  # 单个 commit 解析失败不阻断整体
        category = _guess_category(subject)
        for rel_path, start, end in _parse_diff_hunks(diff_text):
            if not _is_source_candidate(rel_path):
                continue
            goldens.append(
                GoldenIssue(
                    project=project_name,
                    file=rel_path.replace("\\", "/"),
                    line_start=start,
                    line_end=end,
                    category=category,
                    severity="high",
                    description=f"[git-history] {subject} :: {rel_path} L{start}-L{end}",
                    origin="git-history",
                )
            )
    if goldens:
        save_goldset(goldens, out_jsonl)
    return goldens


# ---------------------------------------------------------------- 缺陷注入


@dataclass(frozen=True)
class _DefectTemplate:
    """一个缺陷注入模板：函数体（``{name}`` 为唯一函数名占位）+ 对应规则元数据。"""

    key: str
    rule_id: str
    category: str
    severity: str
    body: str

    def render(self, name: str) -> str:
        """渲染为函数源码文本（不带末尾换行）。"""
        return self.body.format(name=name)


DEFECT_TEMPLATES: tuple[_DefectTemplate, ...] = (
    _DefectTemplate(
        key="bare_except",
        rule_id="PY-BARE-EXCEPT",
        category="bug",
        severity="medium",
        body=(
            "def {name}(raw_value):\n"
            "    try:\n"
            "        return int(raw_value)\n"
            "    except:\n"
            "        return 0"
        ),
    ),
    _DefectTemplate(
        key="mutable_default",
        rule_id="PY-MUTABLE-DEFAULT",
        category="bug",
        severity="high",
        body=(
            "def {name}(items, bucket=[]):\n"
            "    for item in items:\n"
            "        bucket.append(item)\n"
            "    return bucket"
        ),
    ),
    _DefectTemplate(
        key="eq_none",
        rule_id="PY-EQ-NONE",
        category="bug",
        severity="low",
        body=(
            "def {name}(value):\n"
            "    if value == None:\n"
            "        return False\n"
            "    return True"
        ),
    ),
    _DefectTemplate(
        key="sql_concat",
        rule_id="PY-SQL-INJECTION",
        category="security",
        severity="critical",
        body=(
            "def {name}(conn, user_id):\n"
            "    query = \"SELECT * FROM users WHERE id = \" + user_id\n"
            "    return conn.execute(query)"
        ),
    ),
    _DefectTemplate(
        key="hardcoded_secret",
        rule_id="PY-HARDCODED-SECRET",
        category="security",
        severity="critical",
        body=(
            "def {name}():\n"
            "    access_token = \"sk-live-9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c\"\n"
            "    return access_token"
        ),
    ),
    _DefectTemplate(
        key="urlopen_no_timeout",
        rule_id="PY-NO-TIMEOUT",
        category="performance",
        severity="medium",
        body=(
            "def {name}(url):\n"
            "    import urllib.request\n"
            "\n"
            "    with urllib.request.urlopen(url) as resp:\n"
            "        return resp.read()"
        ),
    ),
    _DefectTemplate(
        key="list_membership",
        rule_id="PY-LIST-MEMBERSHIP",
        category="performance",
        severity="medium",
        body=(
            "def {name}(items):\n"
            "    seen = []\n"
            "    for item in items:\n"
            "        if item in seen:\n"
            "            continue\n"
            "        seen.append(item)\n"
            "    return seen"
        ),
    ),
    _DefectTemplate(
        key="str_concat_loop",
        rule_id="PY-STR-CONCAT-LOOP",
        category="performance",
        severity="low",
        body=(
            "def {name}(rows):\n"
            "    html = \"\"\n"
            "    for row in rows:\n"
            "        html = html + \"<li>\" + str(row) + \"</li>\"\n"
            "    return html"
        ),
    ),
)


def _candidate_py_files(src_root: Path) -> list[Path]:
    """收集可注入的 .py 文件（相对路径，排序保证确定性）：跳过测试/__init__/隐藏目录。"""
    out: list[Path] = []
    for path in sorted(src_root.rglob("*.py")):
        rel = path.relative_to(src_root)
        parts = rel.parts
        if any(p.startswith(".") for p in parts):
            continue
        name = path.name
        if name in {"__init__.py", "conftest.py", "setup.py"}:
            continue
        if name.startswith("test_") or name.endswith("_test.py"):
            continue
        if any(p in {"tests", "test"} for p in parts[:-1]):
            continue
        out.append(rel)
    return out


def inject_defects(
    src_project: Path,
    out_project: Path,
    n: int = 10,
    seed: int = 0,
    per_file: int = 3,
) -> list[GoldenIssue]:
    """把源项目复制到 out_project 并向选中的 .py 文件追加已知缺陷函数。

    - 随机（``seed`` 可复现）选 ≤ n 个候选 .py 文件，每个文件注入
      ``per_file`` 个（默认 3，≤ 模板总数且不重复）模板；
    - 模板以独立函数形式**追加**到文件末尾，不改动原文件任何已有内容；
    - 每次注入后整体 ``ast.parse`` 校验，失败则回退该次注入（跳过）；
    - GoldenIssue 行区间 = 注入函数在结果文件中的精确 def 行～函数末行。
    """
    src_project, out_project = Path(src_project), Path(out_project)
    if src_project.resolve() == out_project.resolve():
        raise ValueError("out_project 不能与 src_project 相同目录")
    if out_project.exists():
        shutil.rmtree(out_project)
    shutil.copytree(src_project, out_project)

    rng = random.Random(seed)
    candidates = _candidate_py_files(src_project)
    rng.shuffle(candidates)
    selected = candidates[: max(0, n)]

    goldens: list[GoldenIssue] = []
    project_name = out_project.name
    for rel in selected:
        target = out_project / rel
        original = target.read_text(encoding="utf-8")
        out_lines = original.splitlines()
        cursor = len(out_lines)  # 已有完整行数；追加的下一物理行号为 cursor+1
        counter = 0
        templates = rng.sample(DEFECT_TEMPLATES, min(max(1, per_file), len(DEFECT_TEMPLATES)))
        for tpl in templates:
            counter += 1
            func_name = f"_inj_{tpl.key}_{counter}"
            body = tpl.render(func_name).splitlines()
            trial_lines = out_lines + [""] + body
            try:
                ast.parse("\n".join(trial_lines) + "\n")
            except SyntaxError:
                continue  # 注入导致语法失效：跳过该模板
            out_lines = trial_lines
            cursor += 1
            start = cursor + 1
            cursor += len(body)
            goldens.append(
                GoldenIssue(
                    project=project_name,
                    file=rel.as_posix(),
                    line_start=start,
                    line_end=cursor,
                    category=tpl.category,
                    severity=tpl.severity,
                    # 描述含文件路径：同一函数名后缀会出现在多个文件中，
                    # 而 detection_metrics 按 description 去重，必须全局唯一
                    description=f"[injected:{tpl.key}] {rel.as_posix()}:{start} 注入缺陷函数 {func_name}（{tpl.rule_id}）",
                    origin="injected",
                )
            )
        target.write_text("\n".join(out_lines) + "\n", encoding="utf-8")

    goldens.sort(key=lambda g: (g.file, g.line_start))
    return goldens
