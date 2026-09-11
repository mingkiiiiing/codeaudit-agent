"""Stage1 接入：目录 / zip → 工作副本 + FileManifest。

职责：
  - 在 work_root/{audit_id}/src 物化工作副本（zip 安全解压 + 单层顶层目录提升）
  - 物化阶段即应用内置忽略目录（R1-11：copytree ignore，避免把 node_modules/
    .git 等大目录复制进副本），被忽略文件不进入工作副本
  - 产出 FileManifest（path/language/loc/sha256），识别主语言
  - 规模保护：超过文件数 / 行数阈值抛 IngestError（阈值可参数化）
  - 失败清理（R1-16）：接入失败时删除半成品 task_root，不留垃圾目录
"""

from __future__ import annotations

import hashlib
import os
import shutil
import zipfile
from pathlib import Path, PurePosixPath

from audit.errors import IngestError
from audit.ingest.decode import decode_source_bytes
from audit.ingest.filters import EXTRA_IGNORE_DIRS, GitignoreSet, should_ignore
from audit.models import FileManifest
from audit.utils import count_lines, guess_language, new_audit_id
from audit.workspace import DEFAULT_IGNORE_DIRS, WorkspaceContext

# 单个源文件行数上限（docs/02 Stage1：超长文件跳过并记录）
DEFAULT_MAX_FILE_LINES = 10_000

# zip 解压累计上限（解压炸弹防御，R1-14）：超限抛 IngestError
DEFAULT_MAX_ZIP_BYTES = 1_000_000_000  # 1 GB 未压缩总量

# 物化阶段整体跳过的目录（内置忽略 + zip 垃圾目录）
_MATERIALIZE_IGNORE_DIRS = DEFAULT_IGNORE_DIRS | EXTRA_IGNORE_DIRS


def ingest(
    source: str | Path,
    work_root: Path,
    audit_id: str | None = None,
    *,
    max_files: int = 2000,
    max_loc: int = 500_000,
    max_file_lines: int = DEFAULT_MAX_FILE_LINES,
    max_zip_bytes: int = DEFAULT_MAX_ZIP_BYTES,
) -> WorkspaceContext:
    """把 source（目录或 .zip）接入为独立工作副本，返回 WorkspaceContext。

    - 目录入口：整树复制（跳过内置忽略目录）后按过滤规则剪枝
    - zip 入口：安全解压（拒绝路径逃逸成员；累计解压大小前置校验），若所有条目
      共用单个顶层目录则提升
    - 产出 ctx.manifests 与 ctx.language；规模超限抛 audit.errors.IngestError
    - 任何失败都会清理半成品 task_root（R1-16）后原样抛出
    """
    src = _resolve_source(source)
    if audit_id is None:
        audit_id = new_audit_id()

    work_root = Path(work_root)
    task_root = work_root / audit_id
    src_root = task_root / "src"
    if src_root.exists():
        shutil.rmtree(src_root)
    src_root.mkdir(parents=True, exist_ok=True)

    try:
        if src.is_dir():
            _materialize_dir(src, src_root)
        else:
            _materialize_zip(src, src_root, max_zip_bytes=max_zip_bytes)
            # 仅 zip 入口提升单层顶层目录（目录入口的 src/ 子目录是正常结构）
            _hoist_single_top_dir(src_root)

        gitignore = GitignoreSet.collect(src_root)

        manifests = _build_manifests(src_root, gitignore, max_file_lines=max_file_lines)
        _prune_empty_dirs(src_root)
        if not manifests:
            raise IngestError(f"接入后没有可用源文件：{src}")

        total_loc = sum(m.loc for m in manifests)
        if len(manifests) > max_files:
            raise IngestError(f"文件数 {len(manifests)} 超过上限 {max_files}")
        if total_loc > max_loc:
            raise IngestError(f"总行数 {total_loc} 超过上限 {max_loc}")
    except BaseException:
        shutil.rmtree(task_root, ignore_errors=True)  # R1-16：失败不留半成品
        raise

    return WorkspaceContext(
        audit_id=audit_id,
        src_root=src_root,
        work_root=task_root,
        db_path=task_root / "index.db",
        language=_detect_main_language(manifests),
        manifests=manifests,
    )


# ---------------------------------------------------------------- 源解析


def _resolve_source(source: str | Path) -> Path:
    src = Path(source)
    if not src.exists():
        raise IngestError(f"源不存在：{src}")
    if src.is_dir():
        return src
    if src.is_file():
        return src  # 视为 zip，材质化阶段校验
    raise IngestError(f"不支持的源类型：{src}")


# ---------------------------------------------------------------- 材质化


def _materialize_dir(src: Path, src_root: Path) -> None:
    """整树复制；物化阶段即跳过内置忽略目录（R1-11：不把 .git/node_modules 等搬进副本）。"""
    shutil.copytree(
        src,
        src_root,
        dirs_exist_ok=True,
        ignore=lambda _dir, names: [n for n in names if n in _MATERIALIZE_IGNORE_DIRS],
    )


def _materialize_zip(src: Path, src_root: Path, *, max_zip_bytes: int) -> None:
    try:
        zf = zipfile.ZipFile(src)
    except (zipfile.BadZipFile, OSError) as exc:
        raise IngestError(f"zip 打开失败：{src}（{exc}）") from exc
    with zf:
        infos = [info for info in zf.infolist() if not info.is_dir()]
        # R1-14：解压前按中央目录声明大小做累计校验（解压炸弹防御）
        declared_total = 0
        for info in infos:
            name = info.filename
            pure = PurePosixPath(name)
            if pure.is_absolute() or ".." in pure.parts or (pure.parts and pure.parts[0].endswith(":")):
                continue  # 路径逃逸成员不计入
            declared_total += int(info.file_size)
        if declared_total > max_zip_bytes:
            raise IngestError(
                f"zip 解压总量 {declared_total} 字节超过上限 {max_zip_bytes}（解压炸弹防御）"
            )
        for info in infos:
            name = info.filename
            pure = PurePosixPath(name)
            # 路径逃逸防护：绝对路径 / .. 段 / 盘符一律跳过
            if pure.is_absolute() or ".." in pure.parts or (pure.parts and pure.parts[0].endswith(":")):
                continue
            if any(part in _MATERIALIZE_IGNORE_DIRS for part in pure.parts):
                continue  # R1-11：与目录入口同口径，忽略目录成员不进副本
            try:
                zf.extract(info, src_root)
            except (OSError, ValueError) as exc:
                raise IngestError(f"zip 成员解压失败：{name}（{exc}）") from exc


def _hoist_single_top_dir(src_root: Path) -> None:
    """zip 常见形态：所有内容在一个顶层目录里 → 提升一层作为项目根。"""
    children = [c for c in src_root.iterdir()]
    if len(children) == 1 and children[0].is_dir():
        inner = children[0]
        for child in inner.iterdir():
            shutil.move(str(child), str(src_root / child.name))
        inner.rmdir()


def _prune_empty_dirs(src_root: Path) -> None:
    """自底向上删除剪枝后遗留的空目录（保留 src_root 本身）。"""
    for dirpath, dirnames, _filenames in list(os.walk(src_root, topdown=False)):
        p = Path(dirpath)
        if p == src_root:
            continue
        try:
            if not any(p.iterdir()):
                p.rmdir()
        except OSError:
            continue


# ---------------------------------------------------------------- 清单


def _build_manifests(src_root: Path, gitignore: GitignoreSet, *, max_file_lines: int) -> list[FileManifest]:
    """遍历工作副本：应用过滤规则剪枝，产出 FileManifest 列表。

    R1-15：每个文件只读盘一次（read_bytes），字节流同时用于 sha256 与解码/行数，
    避免逐文件两次 IO。
    """
    manifests: list[FileManifest] = []
    for path in sorted(src_root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(src_root)
        if should_ignore(rel, gitignore):
            path.unlink(missing_ok=True)  # 工作副本保持"过滤后"视图
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue  # 读取失败（如被外部删除）不阻断接入
        text = decode_source_bytes(data)
        loc = count_lines(text)
        manifest = FileManifest(
            path=rel.as_posix(),
            language=guess_language(rel) or "",
            loc=loc,
            sha256=hashlib.sha256(data).hexdigest(),
        )
        if loc > max_file_lines:
            # 超长文件：保留在磁盘但记录跳过原因，索引阶段不再解析
            manifest.parsed_ok = False
            manifest.skipped_reason = f"too_long(>{max_file_lines} lines)"
        manifests.append(manifest)
    return manifests


def _detect_main_language(manifests: list[FileManifest]) -> str:
    """按扩展名统计主语言（仅统计可识别语言）。"""
    counts: dict[str, int] = {}
    for m in manifests:
        if m.language:
            counts[m.language] = counts.get(m.language, 0) + 1
    if not counts:
        return ""
    # 计数降序、名称升序，保证确定性
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
