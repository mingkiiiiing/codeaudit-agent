"""W28-A CLI rename 子命令自测（全部离线零 mock，dogfood 语料拷贝到 tmp 真实调用）。

覆盖：默认 dry-run 不落盘 + 「预览」标注 / --yes 落盘且代码区旧名零残留 /
多定义点与非法名拒绝（不落盘）/ 源路径不存在退出 1 / --diff-only 语义别名。
语料复用 tests/unit/refactor/fixtures/rename_dogfood/（与
tests/unit/refactor/test_rename.py 的 dogfood 门同源，拷贝执行不碰本体）。
"""

from __future__ import annotations

import shutil
import tokenize
from pathlib import Path

import cli

DOGFOOD = Path(__file__).resolve().parents[1] / "refactor" / "fixtures" / "rename_dogfood"


# ---------------------------------------------------------------- 夹具工具
def _copy_dogfood(tmp_path: Path) -> Path:
    """把 dogfood 语料拷到独立临时副本（测试绝不改动仓库内的语料本体）。"""
    proj = tmp_path / "proj"
    shutil.copytree(DOGFOOD, proj)
    return proj


def _code_ident_count(proj: Path, name: str) -> int:
    """代码区标识符计数（tokenize NAME token 口径；字符串/注释不计数）。"""
    total = 0
    for py in sorted(proj.rglob("*.py")):
        with py.open("rb") as fh:
            for tok in tokenize.tokenize(fh.readline):
                if tok.type == tokenize.NAME and tok.string == name:
                    total += 1
    return total


def _snapshot(proj: Path) -> dict[Path, bytes]:
    return {py: py.read_bytes() for py in sorted(proj.rglob("*.py"))}


# ---------------------------------------------------------------- dry-run（默认）
def test_rename_dry_run_default_no_write_and_preview_tag(tmp_path: Path, capsys):
    proj = _copy_dogfood(tmp_path)
    snap = _snapshot(proj)
    before = _code_ident_count(proj, "calc_total")
    assert before > 0

    rc = cli.main(["rename", str(proj), "calc_total", "compute_total"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "预览模式（未落盘，加 --yes 应用）" in out  # 信任与可控：预览标注
    assert "diff" in out and "+def compute_total" in out  # 逐文件 unified diff
    for py, data in snap.items():
        assert py.read_bytes() == data  # 默认不落盘
    assert _code_ident_count(proj, "calc_total") == before  # 代码区旧名原样


def test_rename_diff_only_alias_same_as_dry_run(tmp_path: Path, capsys):
    proj = _copy_dogfood(tmp_path)
    snap = _snapshot(proj)

    rc = cli.main(["rename", str(proj), "calc_total", "compute_total", "--diff-only"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "预览模式（未落盘，加 --yes 应用）" in out
    assert "+def compute_total" in out
    for py, data in snap.items():
        assert py.read_bytes() == data


def test_rename_diff_only_conflicts_with_yes(tmp_path: Path, capsys):
    proj = _copy_dogfood(tmp_path)

    rc = cli.main(["rename", str(proj), "calc_total", "compute_total", "--diff-only", "--yes"])

    assert rc == 1
    assert "互斥" in capsys.readouterr().err
    assert "def calc_total" in (proj / "util.py").read_text(encoding="utf-8")  # 未落盘


# ---------------------------------------------------------------- --yes 落盘
def test_rename_yes_writes_source_zero_code_residue(tmp_path: Path, capsys):
    proj = _copy_dogfood(tmp_path)
    before = _code_ident_count(proj, "calc_total")

    rc = cli.main(["rename", str(proj), "calc_total", "compute_total", "--yes"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "已应用（写入源码）" in out
    assert "已写入" in out
    assert "写入 2 个文件" in out  # util.py（定义）+ 引用方文件
    # 代码区旧名零残留、新名引用数守恒（token 级口径）
    assert _code_ident_count(proj, "calc_total") == 0
    assert _code_ident_count(proj, "compute_total") == before
    util = (proj / "util.py").read_text(encoding="utf-8")
    assert "def compute_total(items):" in util
    assert "calc_total 汇总金额" in util  # docstring 陷阱保留旧名（token 级不越界）


# ---------------------------------------------------------------- 计划阶段拒绝
def test_rename_multiple_definitions_rejected_no_write(tmp_path: Path, capsys):
    # dogfood 语料的 Validator 在 models.py 与 validation.py 各有一个定义点（多定义反例）
    proj = _copy_dogfood(tmp_path)
    snap = _snapshot(proj)

    rc = cli.main(["rename", str(proj), "Validator", "Checker", "--yes"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "同名定义点" in err and "宁拒不改" in err
    for py, data in snap.items():
        assert py.read_bytes() == data  # 拒绝即不落盘


def test_rename_illegal_name_rejected_no_write(tmp_path: Path, capsys):
    proj = _copy_dogfood(tmp_path)
    snap = _snapshot(proj)

    rc = cli.main(["rename", str(proj), "calc_total", "not-an-ident"])

    assert rc == 1
    assert "合法 python 标识符" in capsys.readouterr().err
    for py, data in snap.items():
        assert py.read_bytes() == data  # 非法名不落盘


# ---------------------------------------------------------------- 防御
def test_rename_source_missing_exit_1(tmp_path: Path, capsys):
    rc = cli.main(["rename", str(tmp_path / "no_such_dir"), "foo", "bar"])

    assert rc == 1
    assert "源路径不存在" in capsys.readouterr().err
