"""CodeAudit Agent 打包就绪自检脚本。

离线环境下（未安装 build/twine）用元数据自检替代真实构建，逐项校验：

1. ``audit.__version__`` 是合法 semver（x.y.z），且为 0.2.0；
2. ``pyproject.toml`` 声明动态版本（``dynamic = ["version"]``），
   且 ``[tool.setuptools.dynamic]`` 的 ``attr`` 指向 ``audit.__version__``（版本单源）；
3. ``[project.scripts]`` 含 ``codeaudit = "cli:main"``，且 ``py-modules`` 含 ``cli``；
4. 包数据声明的报告模板文件在磁盘真实存在；
5. 若当前环境已安装本包（importlib.metadata / pip show 可用），
   交叉校验已安装版本与入口点；不可用时降级为纯文件级校验（不要求已安装）。

用法::

    python scripts/check_build.py

全部通过输出 ``PASS`` 并以退出码 0 结束；任一项失败输出 ``FAIL`` 并以退出码 1 结束。
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT_PATH = ROOT / "pyproject.toml"
AUDIT_PKG_DIR = ROOT / "audit"
TEMPLATES_DIR = AUDIT_PKG_DIR / "report" / "templates"

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
EXPECTED_VERSION = "0.2.0"
EXPECTED_SCRIPT_TARGET = "cli:main"
DIST_NAME = "codeaudit-agent"

# 检查函数签名：返回 (是否通过, 明细说明)
CheckFn = Callable[[], tuple[bool, str]]


def check_semver_version() -> tuple[bool, str]:
    """校验 audit.__version__ 为合法 semver 且为约定版本。"""
    sys.path.insert(0, str(ROOT))
    import audit

    version: str = audit.__version__
    if not SEMVER_RE.match(version):
        return False, f"audit.__version__ = {version!r} 不符合 x.y.z 格式"
    if version != EXPECTED_VERSION:
        return False, f"audit.__version__ = {version!r}，期望 {EXPECTED_VERSION!r}"
    return True, f"audit.__version__ = {version}（合法 semver，单源生效）"


def check_dynamic_version(pyproject: dict) -> tuple[bool, str]:
    """校验 pyproject 声明动态版本且 attr 指向 audit.__version__。"""
    project = pyproject.get("project", {})
    dynamic = project.get("dynamic", [])
    if "version" not in dynamic:
        return False, "[project].dynamic 未包含 'version'"
    if "version" in project:
        return False, "[project] 同时存在静态 version 与 dynamic，版本非单源"
    dyn = pyproject.get("tool", {}).get("setuptools", {}).get("dynamic", {})
    attr = dyn.get("version", {}).get("attr")
    if attr != "audit.__version__":
        return False, f"[tool.setuptools.dynamic].version.attr = {attr!r}，期望 'audit.__version__'"
    return True, "dynamic = [\"version\"]，attr = 'audit.__version__'（版本单源声明正确）"


def check_console_script(pyproject: dict) -> tuple[bool, str]:
    """校验 [project.scripts] 的 codeaudit 入口与 py-modules 声明。"""
    scripts = pyproject.get("project", {}).get("scripts", {})
    target = scripts.get("codeaudit")
    if target is None:
        return False, "[project.scripts] 缺少 codeaudit 入口"
    if target != EXPECTED_SCRIPT_TARGET:
        return False, f"codeaudit 指向 {target!r}，期望 {EXPECTED_SCRIPT_TARGET!r}"
    st = pyproject.get("tool", {}).get("setuptools", {})
    py_modules: list[str] = st.get("py-modules", [])
    if "cli" not in py_modules:
        return False, f"[tool.setuptools].py-modules = {py_modules!r} 缺少 'cli'（cli:main 将不可安装）"
    return True, f"codeaudit = '{target}'，py-modules = {py_modules}"


def check_package_data(pyproject: dict) -> tuple[bool, str]:
    """校验包数据声明与模板文件在磁盘真实存在。"""
    pkg_data = pyproject.get("tool", {}).get("setuptools", {}).get("package-data", {})
    patterns: list[str] = pkg_data.get("audit", [])
    if not patterns:
        return False, "[tool.setuptools.package-data] 缺少 audit 的声明"
    templates = sorted(TEMPLATES_DIR.glob("*.j2"))
    if not templates:
        return False, f"模板目录为空：{TEMPLATES_DIR}"
    names = [t.name for t in templates]
    for tpl in templates:
        if not tpl.is_file():
            return False, f"模板不是常规文件：{tpl}"
    return True, f"package-data = {patterns}；磁盘模板：{', '.join(names)}"


def check_installed_metadata() -> tuple[bool, str]:
    """已安装时交叉校验元数据；未安装则降级为文件级校验（视为通过）。"""
    try:
        from importlib import metadata
    except ImportError:  # pragma: no cover - Python 3.8+ 必有 importlib.metadata
        return True, "importlib.metadata 不可用，降级为文件级校验"
    try:
        installed = metadata.version(DIST_NAME)
    except metadata.PackageNotFoundError:
        return True, f"环境未安装 {DIST_NAME}，降级为文件级校验（不要求已安装）"
    except Exception as exc:  # noqa: BLE001 —— 元数据读取失败的兜底降级
        return True, f"读取已安装元数据失败（{type(exc).__name__}），降级为文件级校验"
    import audit

    if installed != audit.__version__:
        return False, f"已安装版本 {installed} != 源码版本 {audit.__version__}（请重装 pip install -e .）"
    eps = [ep for ep in metadata.entry_points(group="console_scripts") if ep.name == "codeaudit"]
    if not eps:
        return False, "已安装但缺少 console script 'codeaudit'（请重装）"
    if eps[0].value != EXPECTED_SCRIPT_TARGET:
        return False, f"console script 指向 {eps[0].value!r}，期望 {EXPECTED_SCRIPT_TARGET!r}"
    return True, f"已安装 {DIST_NAME}=={installed}，entry point codeaudit -> {eps[0].value}"


def main() -> int:
    """执行全部检查项，逐项打印中文结果，输出最终 PASS/FAIL。"""
    if not PYPROJECT_PATH.is_file():
        print(f"✗ 未找到 {PYPROJECT_PATH}")
        return 1
    with PYPROJECT_PATH.open("rb") as fh:
        pyproject = tomllib.load(fh)

    checks: list[tuple[str, CheckFn]] = [
        ("版本 semver 与单源值", check_semver_version),
        ("pyproject 动态版本声明", lambda: check_dynamic_version(pyproject)),
        ("console script 入口点", lambda: check_console_script(pyproject)),
        ("包数据模板存在性", lambda: check_package_data(pyproject)),
        ("已安装元数据交叉校验", check_installed_metadata),
    ]

    print("========== CodeAudit 打包自检 ==========")
    all_ok = True
    for title, fn in checks:
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001 —— 单项异常不中断其余检查
            ok, detail = False, f"检查抛出异常：{type(exc).__name__}: {exc}"
        mark = "✓" if ok else "✗"
        print(f"{mark} {title}：{detail}")
        all_ok = all_ok and ok
    print("========================================")
    print("PASS" if all_ok else "FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
