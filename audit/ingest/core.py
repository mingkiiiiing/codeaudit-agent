"""Stage1 接入：目录 / zip → 工作副本 + FileManifest。

职责：
  - 在 work_root/{audit_id}/src 物化工作副本（zip 安全解压 + 单层顶层目录提升）
  - 物化阶段即应用内置忽略目录（R1-11：copytree ignore，避免把 node_modules/
    .git 等大目录复制进副本），被忽略文件不进入工作副本
  - 目录入口同盘时优先 os.link 硬链接物化（W7-A1，docs/12 §2）：省去整树拷贝
    的写盘开销；跨盘 / 链接失败（OSError EPERM/EXDEV 等）逐文件回退 copy，
    结构性失败整体回退 copytree，manifests 与拷贝路径完全一致
  - 产出 FileManifest（path/language/loc/sha256），识别主语言
  - 规模保护：超过文件数 / 行数阈值抛 IngestError（阈值可参数化）
  - 失败清理（R1-16）：接入失败时删除半成品 task_root，不留垃圾目录

⚠️ 硬链接语义说明（W7-A1）：工作副本与源文件共享 inode。只读流水线（ingest/
index/understand/detect/report）不受影响；但 fix 阶段的 git apply 会**就地改写**
工作副本文件——启用 do_fix 的审计建议以 link_same_volume=False 接入（回退整树
拷贝），或在集成层把工作副本视为可丢弃副本（源为一次性拷贝，如 demo 流程）。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import zipfile
from pathlib import Path

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

# 解压分块大小（W14-A1，Minor-6：分块读写以便累计实际写出量）
_ZIP_CHUNK = 1024 * 1024

# Windows 保留设备名（W14-A1，Minor-7）：这些名字（含带扩展名形态如 CON.txt、
# 目录形态 CON/）在 Windows 上是设备而非普通文件，写入会失败或指向设备流，
# zip 入口一律跳过。判定口径：路径段首个 `.` 前的主名（不分大小写）命中即拒。
_WIN_RESERVED_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

# 与 zipfile.extract 成员名净化同口径：Windows 非法字符替换为 `_`
_WIN_ILLEGAL_TRANS = str.maketrans(
    {
        **{chr(c): "_" for c in range(0x20)},
        **{chr(c): "_" for c in range(0x7F, 0x100)},
        **dict.fromkeys(map(ord, '<>:"|?*'), "_"),
    }
)


def _is_windows_reserved_name(part: str) -> bool:
    """路径段是否为 Windows 保留设备名（含 CON.txt 这类带扩展名形态）。"""
    return part.split(".", 1)[0].upper() in _WIN_RESERVED_DEVICE_NAMES


def _safe_member_parts(name: str) -> list[str] | None:
    """zip 成员名 → 工作副本内相对路径段列表；不安全或不入副本的成员返回 None。

    W14-A1（Minor-6/7）把预校验与解压两阶段统一到同一过滤口径：
    - 绝对路径 / `..` 段 / 首段盘符形态：路径逃逸，拒；
    - Windows 保留设备名段（CON/PRN/AUX/NUL/COM1-9/LPT1-9，含 CON.txt 形态）：拒；
    - 内置忽略目录段（R1-11，与目录入口同口径）：跳过；
    - 其余段按 zipfile.extract 同口径净化（非法字符→`_`，剥尾部点/空格），
      反斜杠视作路径分隔符（与 Windows 实际语义一致，防混合分隔符逃逸）。
    """
    if name.startswith(("/", "\\")):
        return None  # 绝对路径
    parts: list[str] = []
    for idx, part in enumerate(name.replace("\\", "/").split("/")):
        if part in ("", "."):
            continue
        if part == ".." or (idx == 0 and part.endswith(":")):
            return None  # 上跳段 / 盘符段：路径逃逸（与既有过滤同口径）
        if _is_windows_reserved_name(part):
            return None  # Minor-7：Windows 保留设备名不落盘
        if part in _MATERIALIZE_IGNORE_DIRS:
            return None  # R1-11：忽略目录成员不进副本
        part = part.translate(_WIN_ILLEGAL_TRANS).rstrip(" .")
        if not part:
            return None
        parts.append(part)
    return parts or None

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
    link_same_volume: bool = False,  # W7 实测硬链接路径在本机无收益，默认整树复制（可显式开启）
) -> WorkspaceContext:
    """把 source（目录或 .zip）接入为独立工作副本，返回 WorkspaceContext。

    - 目录入口：同盘优先整树硬链接（W7-A1，link_same_volume=True 默认）；跨盘 /
      链接失败逐文件回退 copy，结构性失败整体回退整树复制（跳过内置忽略目录），
      三条路径产出的 manifests 完全一致
    - link_same_volume=False：禁用硬链接，始终整树复制（do_fix 审计的推荐姿势，
      见模块 docstring 的硬链接语义说明）
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
            _materialize_dir(src, src_root, link_same_volume=link_same_volume)
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


def _materialize_dir(src: Path, src_root: Path, *, link_same_volume: bool = True) -> None:
    """整树物化工作副本；物化阶段即跳过内置忽略目录（R1-11：不把 .git/node_modules 等搬进副本）。

    W7-A1：同盘优先 os.link 硬链接（省整树拷贝写盘）；链接失败逐文件回退 copy、
    结构性失败整体回退 copytree——两条路径对后续阶段（gitignore 剪枝/manifests/
    索引）完全等价。link_same_volume=False 时直接走复制路径。
    """
    if link_same_volume and _try_hardlink_tree(src, src_root):
        return
    shutil.copytree(
        src,
        src_root,
        dirs_exist_ok=True,
        ignore=lambda _dir, names: [n for n in names if n in _MATERIALIZE_IGNORE_DIRS],
    )


def _same_volume(a: Path, b: Path) -> bool:
    """同卷判定：st_dev 相同且盘符（anchor）一致（Windows 下 st_dev 即卷标识）。"""
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev and Path(a).anchor == Path(b).anchor
    except OSError:
        return False


def _try_hardlink_tree(src: Path, src_root: Path) -> bool:
    """同盘硬链接整树（W7-A1）。成功返回 True；结构性失败返回 False（调用方回退 copytree）。

    - 忽略目录语义与 copytree ignore 完全一致（_MATERIALIZE_IGNORE_DIRS 不进副本）；
    - 单文件 os.link 失败（跨盘残留 / EPERM / 文件系统不支持硬链接）逐文件回退
      shutil.copy2，物化结果不受影响；
    - 符号链接按 copytree 默认语义（symlinks=False）跟随：链接文件 copy2 内容、
      链接目录递归进入（带 st_dev/st_ino 已访问环防护）。
    """
    if not _same_volume(src, src_root):
        return False
    visited_dirs: set[tuple[int, int]] = set()  # (st_dev, st_ino) 防符号链接目录成环
    try:
        for dirpath, dirnames, filenames in os.walk(src, followlinks=True):
            cur = Path(dirpath)
            try:
                st = os.stat(cur)
                key = (st.st_dev, st.st_ino)
                if key in visited_dirs:
                    dirnames[:] = []  # 环：剪掉整棵子树
                    continue
                visited_dirs.add(key)
            except OSError:
                return False
            dirnames[:] = [d for d in dirnames if d not in _MATERIALIZE_IGNORE_DIRS]
            dst_dir = src_root / cur.relative_to(src)
            dst_dir.mkdir(parents=True, exist_ok=True)
            for name in filenames:
                s = cur / name
                d = dst_dir / name
                if d.exists():  # 幂等：重跑同 audit_id 时先清旧入口
                    d.unlink()
                try:
                    if s.is_symlink():
                        shutil.copy2(s, d, follow_symlinks=True)  # 与 copytree(symlinks=False) 同语义
                    else:
                        os.link(s, d)
                except OSError:
                    try:  # 逐文件回退 copy（EPERM/EXDEV/文件系统不支持等）
                        shutil.copy2(s, d)
                    except OSError:
                        return False
        return True
    except OSError:
        return False


def _materialize_zip(src: Path, src_root: Path, *, max_zip_bytes: int) -> None:
    try:
        zf = zipfile.ZipFile(src)
    except (zipfile.BadZipFile, OSError) as exc:
        raise IngestError(f"zip 打开失败：{src}（{exc}）") from exc
    with zf:
        infos = [info for info in zf.infolist() if not info.is_dir()]
        # R1-14：解压前按中央目录声明大小做累计校验（解压炸弹防御前置快筛）
        declared_total = 0
        for info in infos:
            if _safe_member_parts(info.filename) is None:
                continue  # 不入副本的成员不计入
            declared_total += max(0, int(info.file_size))
        if declared_total > max_zip_bytes:
            raise IngestError(
                f"zip 解压总量 {declared_total} 字节超过上限 {max_zip_bytes}（解压炸弹防御）"
            )
        extracted_total = 0
        for info in infos:
            parts = _safe_member_parts(info.filename)
            if parts is None:
                # 路径逃逸 / Windows 保留设备名 / 忽略目录成员一律跳过（无日志，与既有口径一致）
                continue
            target = src_root.joinpath(*parts)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                declared = max(0, int(info.file_size))
                written = 0
                # W14-A1（Minor-6）：中央目录声明值可伪造，解压必须分块复核真实
                # 写出量——单文件实际 > 声明、或累计实际 > 上限，立即中止。
                with zf.open(info) as fsrc, open(target, "wb") as fdst:
                    while True:
                        chunk = fsrc.read(_ZIP_CHUNK)
                        if not chunk:
                            break
                        written += len(chunk)
                        extracted_total += len(chunk)
                        if extracted_total > max_zip_bytes:
                            raise IngestError(
                                f"zip 实际解压总量 {extracted_total} 字节超过上限 {max_zip_bytes}"
                                "（解压炸弹防御）"
                            )
                        if written > declared:
                            raise IngestError(
                                f"zip 成员 {info.filename} 实际解压 {written} 字节超过"
                                f"声明大小 {declared}（解压炸弹防御）"
                            )
                        fdst.write(chunk)
            except IngestError:
                raise
            except (OSError, ValueError, zipfile.BadZipFile) as exc:
                raise IngestError(f"zip 成员解压失败：{info.filename}（{exc}）") from exc


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
        # W19-F3：复制阶段用 is_ignored_copy——lock 清单文件（≤5MB）放行进工作
        # 副本供 depcheck 传递依赖扫描；其余忽略语义与 should_ignore 一致。
        from audit.workspace import is_ignored_copy

        if is_ignored_copy(rel, src_file=path) or should_ignore(rel, gitignore):
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
