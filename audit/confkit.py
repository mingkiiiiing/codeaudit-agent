"""基线与 diff 增量助手（W4-A2，契约 v1.4，见 docs/09 §1 第 3/4 行与 §3）。

能力：
- 基线文件读写：``{"schema_version": 1, "created_at": "...", "fingerprints": [...]}``，
  指纹由 audit.utils.issue_fingerprint 生成（sha1，契约冻结）；
- 基线抑制：命中指纹的问题从最终清单剔除并计数，供 stats.suppressed 使用；
- diff 增量：对 git 仓库跑 ``git diff --name-only <ref>`` 拿变更文件集合，
  并把工作副本（审计器自建的临时副本，删除安全）物理剪枝到变更文件。

风格约定：subprocess 一律 list 参数（禁 shell=True）；所有读取容错，
坏数据返回空结果而不抛异常——基线/增量失败只应回退全量，不应让审计失败。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Iterable, Sequence

from audit.models import Issue
from audit.utils import issue_fingerprint, now_iso
from audit.workspace import WorkspaceContext

__all__ = [
    "BASELINE_SCHEMA_VERSION",
    "apply_baseline",
    "apply_diff_filter",
    "changed_files",
    "load_baseline",
    "write_baseline",
]

# 基线文件格式版本（docs/09 §3，A1 写 / A2 读双方按此实现）
BASELINE_SCHEMA_VERSION = 1

# git 单次 diff 超时（秒）：超大仓库防御，超时按"非 git/失败"处理回退全量
_GIT_TIMEOUT_SEC = 60


# ---------------------------------------------------------------- 基线读写


def write_baseline(issues: Iterable[Issue], out_path: Path, note: str = "") -> Path:
    """把问题清单写为基线文件（docs/09 §3 格式），返回写入路径。

    - 指纹顺序即 issues 顺序（去重由调用方决定，这里不做去重）；
    - note 非空时额外写入 "note" 字段（人读备注，不参与匹配）；
    - 父目录不存在时自动创建。
    """
    fingerprints = [issue_fingerprint(issue) for issue in issues]
    payload: dict[str, Any] = {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "created_at": now_iso(),
        "fingerprints": fingerprints,
    }
    if note:
        payload["note"] = note
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out


def load_baseline(path: Path) -> set[str]:
    """读取基线指纹集合；文件不存在 / 坏 JSON / 字段缺失一律返回空集（不抛异常）。

    基线读取失败应回退"零抑制"而不是让审计失败——这是本函数全容错的原因。
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if not isinstance(data, dict):
        return set()
    fingerprints = data.get("fingerprints")
    if not isinstance(fingerprints, list):
        return set()
    return {str(f) for f in fingerprints if isinstance(f, (str, int)) and str(f)}


def apply_baseline(issues: Sequence[Issue], fingerprints: set[str]) -> tuple[list[Issue], int]:
    """按基线指纹抑制问题，返回 (未命中基线的问题, 被抑制数)。

    指纹命中 = issue_fingerprint(issue) ∈ fingerprints；保持原有相对顺序。
    """
    fingerprints = fingerprints or set()
    kept = [issue for issue in issues if issue_fingerprint(issue) not in fingerprints]
    return kept, len(issues) - len(kept)


# ---------------------------------------------------------------- diff 增量


def changed_files(source_root: Path, ref: str) -> set[str] | None:
    """相对 ref 变更的文件集合（相对仓库根的 posix 路径）。

    - 执行 ``git -C source_root diff --name-only <ref>``（list 参数，禁 shell=True）；
    - ref 以 '-' 开头（R1-22：会被 git 当作选项解析，如 --upload-pack）→ 返回 None；
    - source_root 无 .git、git 不可用/超时/失败 → 返回 None（调用方回退全量）；
    - 未跟踪的新文件不在 git diff 输出内（与 semgrep --diff 行为一致）。
    """
    root = Path(source_root)
    if not ref or ref.startswith("-") or not (root / ".git").exists():
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "-c", "core.quotepath=false", "diff", "--name-only", ref],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT_SEC,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or proc.stdout is None:
        return None
    return {_norm_rel(line) for line in proc.stdout.splitlines() if line.strip()}


def apply_diff_filter(workspace: WorkspaceContext, changed: set[str]) -> int:
    """把工作副本剪枝到变更文件集合，返回保留文件数（同步 manifests 后的值）。

    - 工作副本是审计器在 work_root 下自建的临时副本，物理删除安全；
    - 不在 changed 集合内的文件逐个删除，随后自底向上清理空目录；
    - manifests 同步为保留的变更文件条目（路径归一化匹配）；
    - changed 与工作副本无交集（保留数为 0）时不做任何删除——调用方据
      返回值 0 发"增量集合为空，回退全量"警告，避免把副本删空。
    """
    if not changed:
        return 0
    changed_norm = {_norm_rel(p) for p in changed if _norm_rel(p)}
    manifests = list(workspace.manifests or [])
    if not any(_norm_rel(m.path) in changed_norm for m in manifests):
        return 0  # 无交集：保持原样，由调用方回退全量

    src_root = workspace.src_root
    for path in sorted(src_root.rglob("*")):
        if not path.is_file():
            continue
        rel = _norm_rel(path.relative_to(src_root).as_posix())
        if rel in changed_norm:
            continue
        try:
            path.unlink()
        except OSError:
            continue  # 删除失败不阻断流水线；该文件仍可能被扫描（宁多勿漏）
    _prune_empty_dirs(src_root)

    workspace.manifests = [m for m in manifests if _norm_rel(m.path) in changed_norm]
    return len(workspace.manifests)


# ---------------------------------------------------------------- 内部工具


def _norm_rel(path: str) -> str:
    """把相对路径归一为 posix 风格（容忍反斜杠与 ./ 前缀），供集合匹配。"""
    text = (path or "").replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text.lstrip("/")


def _prune_empty_dirs(root: Path) -> None:
    """自底向上删除剪枝后遗留的空目录（保留 root 本身）。"""
    for dirpath, _dirnames, _filenames in list(os.walk(root, topdown=False)):
        path = Path(dirpath)
        if path == root:
            continue
        try:
            if not any(path.iterdir()):
                path.rmdir()
        except OSError:
            continue
