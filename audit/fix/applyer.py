"""apply-to-source 预览确认回路（P0-2）：把审计产出的 Patch 应用回用户源码。

背景：fix 管线把补丁应用在 <work_root>/<audit_id>/src 工作副本上并验证，回写
用户原始源码此前断头在「用户手工 git apply」。本模块提供安全的回写回路：

- 默认 dry-run：只解析 diff、校验指纹、列出将写入的目标文件与 patch 概要，不落盘；
- --yes（yes=True）才真正写入；
- 原文指纹（Patch.target_sha256，P0-2 补记）：目标文件当前内容 sha256 与审计时不
  一致 → 拒绝该 patch（防覆盖用户后续改动）；任何拒绝 → 整体不落盘（all-or-nothing）；
- 老报告的 Patch 无指纹（target_sha256 为空 dict）：dry-run 可预览，yes 落盘被拒绝
  并提示重新审计；
- 写入用文本精确替换（diff 自带新旧内容，上下文逐行精确匹配，不引入 git 依赖/
  模糊上下文），先写同目录临时文件再 os.replace 原子替换；写入前已对全部目标
  文件留底字节，任一写失败整体回滚，不留半成品。

安全边界：diff 目标路径必须是 workdir 内相对路径（复用 patcher._path_error 的
判定规则，禁绝对路径 / .. / 越出根）；目标文件是符号链接 → 拒绝（规划期与写入
期双重核对：读取会穿透链接、os.replace 会把链接本体替换为普通文件，两种语义
都不该静默发生；已知边界：目标父目录为符号链接的目录逃逸不在本防线内，依赖
路径复检的文本级判定）；目标文件必须可按 UTF-8/GBK 严格解码（二进制内容拒绝
文本替换）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from audit.models import AuditReport, Patch
# 复用 patcher 的 diff 头解析与路径安全判定（同包私有助手，单一事实源）
from audit.fix.patcher import _diff_targets, _header_path, _path_error, _strip_ab_prefix

__all__ = [
    "apply_patches",
    "locate_audit",
    "AuditRecord",
    "ApplyResult",
]

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

_TEMP_SUFFIX = ".codeaudit-apply.tmp"


# ---------------------------------------------------------------- diff 解析


@dataclass
class _Hunk:
    old_start: int  # 1-based
    old_lines: list[str]  # 上下文 + 删除行（去行尾换行）
    new_lines: list[str]  # 上下文 + 新增行（去行尾换行）


@dataclass
class _FilePatch:
    old_rel: str | None  # None = 新增文件（--- /dev/null）
    new_rel: str | None  # None = 删除文件（+++ /dev/null）
    hunks: list[_Hunk] = field(default_factory=list)


def _parse_unified_diff(diff: str) -> tuple[list[_FilePatch], str]:
    """解析 unified diff 为按文件的 hunk 列表；结构非法返回 ([], 原因)。"""
    if not diff or not diff.strip():
        return [], "补丁无 diff 内容"
    lines = diff.splitlines()
    files: list[_FilePatch] = []
    current: _FilePatch | None = None
    hunk: _Hunk | None = None
    old_left = new_left = 0
    for raw in lines:
        if hunk is not None and (old_left > 0 or new_left > 0):
            # hunk 体：按头声明的计数精确消费（上下文/删除计入旧侧，上下文/新增计入新侧）
            if raw.startswith("\\"):  # "\ No newline at end of file" 标记行
                continue
            tag, body = (raw[0], raw[1:]) if raw[:1] in (" ", "-", "+") else (" ", raw)
            if body.endswith("\r"):
                # CRLF 文件的 diff 行尾带 \r（git/生成侧按字节输出），剥离后与
                # 归一化行比较；写出侧再按目标文件主导换行风格补回
                body = body[:-1]
            if tag == " ":
                old_left -= 1
                new_left -= 1
                hunk.old_lines.append(body)
                hunk.new_lines.append(body)
            elif tag == "-":
                old_left -= 1
                hunk.old_lines.append(body)
            else:
                new_left -= 1
                hunk.new_lines.append(body)
            if old_left <= 0 and new_left <= 0:
                hunk = None
            continue
        if raw.startswith(("--- ", "+++ ")):
            raw_path = _header_path(raw)
            rel = None if raw_path is None or raw_path == "/dev/null" else _strip_ab_prefix(raw_path)
            if raw.startswith("--- "):
                current = _FilePatch(old_rel=rel, new_rel=None)
                files.append(current)
            else:
                if current is None:
                    return [], "diff 结构非法：+++ 头前缺少 --- 头"
                current.new_rel = rel
            hunk = None
            continue
        match = _HUNK_RE.match(raw)
        if match:
            if current is None:
                return [], "diff 结构非法：@@ hunk 前缺少 ---/+++ 文件头"
            old_start = int(match.group(1))
            old_count = int(match.group(2)) if match.group(2) is not None else 1
            # new_start/new_count 不直接使用（写入位置由旧侧行号 + 前序 hunk 偏移推导）
            hunk = _Hunk(old_start=old_start, old_lines=[], new_lines=[])
            current.hunks.append(hunk)
            old_left, new_left = old_count, int(match.group(4)) if match.group(4) is not None else 1
            continue
        # 其余行（diff --git / index / 空行等元数据）忽略
    for fp in files:
        if not fp.hunks:
            return [], f"diff 结构非法：文件 {fp.new_rel or fp.old_rel} 无 hunk"
        if fp.old_rel is None and fp.new_rel is None:
            return [], "diff 结构非法：新旧路径同时为 /dev/null"
    return files, ""


# ---------------------------------------------------------------- 文本精确替换


def _decode_text(data: bytes) -> tuple[str, str] | None:
    """严格解码目标文件：utf-8 → gbk；都失败返回 None（拒绝二进制内容）。"""
    for encoding in ("utf-8", "gbk"):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return None


def _apply_file_patch(content: str, fp: _FilePatch) -> tuple[str | None, str]:
    """把单个文件的 hunks 以文本精确替换应用到 content。

    上下文/删除行与目标文件逐行精确匹配（不做 git 的模糊偏移），任何不匹配
    返回 (None, 原因)。未触碰的行保留原始字节形态（含 CRLF），新增行沿用文件的
    主导换行风格；文件末尾是否有换行与原文保持一致。
    """
    if content:
        lines = content.split("\n")
        trailing_newline = lines[-1] == ""
        if trailing_newline:
            lines.pop()
    else:
        lines = []
        trailing_newline = False
    crlf = "\r\n" in content

    def norm(line: str) -> str:
        return line[:-1] if crlf and line.endswith("\r") else line

    norm_lines = [norm(ln) for ln in lines]
    out: list[str] = []
    cursor = 0
    delta = 0  # 前序 hunk 造成的行号偏移
    for hunk in fp.hunks:
        # 旧侧行号语义：old_lines 非空时 old_start 即 hunk 首行（1-based）；
        # old_lines 为空（"@@ -l,0 …" 纯插入）时语义为「插在第 l 行之后」→ 0 基下标 l。
        start = (hunk.old_start - 1 if hunk.old_lines else hunk.old_start) + delta
        if start < cursor or start > len(norm_lines):
            return None, f"hunk 行号越界（旧侧第 {hunk.old_start} 行）"
        if norm_lines[start : start + len(hunk.old_lines)] != hunk.old_lines:
            return None, (
                f"上下文不匹配（旧侧第 {hunk.old_start} 行起）：目标文件内容与补丁"
                "生成时的原文不一致或 diff 与指纹不相符"
            )
        out.extend(lines[cursor:start])
        for added in hunk.new_lines:
            out.append(added + ("\r" if crlf else ""))
        cursor = start + len(hunk.old_lines)
        delta += len(hunk.new_lines) - len(hunk.old_lines)
    out.extend(lines[cursor:])
    result = "\n".join(out)
    if trailing_newline:
        result += "\n"
    return result, ""


def _plan_single_file(
    target_rel: str,
    fp: _FilePatch,
    expected_sha: str,
    workdir: Path,
    mutated: dict[str, bytes],
) -> tuple[bytes | None, str, str]:
    """计算单个目标文件的新内容字节；拒绝返回 (None, 目标展示路径, 原因)。

    mutated：本轮已（拟）写入的同名文件内容（同文件多补丁顺序链的模拟状态），
    命中时指纹核对该内容而非磁盘。
    """
    if target_rel in mutated:
        data = mutated[target_rel]
    else:
        try:
            data = (workdir / target_rel).read_bytes()
        except OSError:
            data = None
    if fp.old_rel is None:  # 新增文件：目标必须不存在
        if data is not None:
            return None, target_rel, f"目标文件 {target_rel} 已存在，但补丁声明新建该文件"
        parts: list[str] = []
        for hunk in fp.hunks:
            parts.extend(hunk.new_lines)
        # "\n".join 后末元素为空串即已含结尾换行；末元素非空补一个结尾换行
        new_text = "\n".join(parts)
        if parts and parts[-1] != "":
            new_text += "\n"
        return new_text.encode("utf-8"), target_rel, ""
    if data is None:
        return None, target_rel, f"目标文件 {target_rel} 不存在（补丁基于已有文件生成）"
    if (Path(workdir) / target_rel).is_symlink():
        return None, target_rel, (
            f"目标文件 {target_rel} 是符号链接，拒绝应用"
            "（写入会静默把链接替换为普通文件）"
        )
    if expected_sha and hashlib.sha256(data).hexdigest() != expected_sha:
        return None, target_rel, (
            f"目标文件 {target_rel} 当前内容 sha256 与审计时不一致"
            f"（预期 {expected_sha[:12]}…，实际 {hashlib.sha256(data).hexdigest()[:12]}…），"
            "审计后源码可能已被修改，拒绝应用以防覆盖"
        )
    if expected_sha == "" and data is not None:
        return None, target_rel, (
            f"目标文件 {target_rel} 已存在，但补丁记录的原文指纹为空（新增文件补丁），拒绝覆盖"
        )
    decoded = _decode_text(data)
    if decoded is None:
        return None, target_rel, f"目标文件 {target_rel} 不是 UTF-8/GBK 文本，拒绝文本替换"
    text, encoding = decoded
    new_text, err = _apply_file_patch(text, fp)
    if new_text is None:
        return None, target_rel, err
    try:
        return new_text.encode(encoding), target_rel, ""
    except (UnicodeEncodeError, LookupError):
        return None, target_rel, f"目标文件 {target_rel} 的新内容无法以 {encoding} 编码写回"


# ---------------------------------------------------------------- 应用结果模型


@dataclass
class ApplyResult:
    """一次 apply 运行的结果（CLI 与 API 共用同一形状）。"""

    dry_run: bool
    applied: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    written_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "rejected": self.rejected,
            "dry_run": self.dry_run,
            "written_files": list(self.written_files),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @property
    def ok(self) -> bool:
        """是否无拒绝（--yes 模式下 ok=False 时未落任何盘）。"""
        return not self.rejected


def _outcome_entry(index: int, patch: Patch, files: list[str], reason: str = "") -> dict[str, Any]:
    entry: dict[str, Any] = {
        "patch_index": index,
        "patch_id": patch.id,
        "issue_id": patch.issue_id,
        "status": patch.apply_status,
        "files": files,
    }
    if reason:
        entry["reason"] = reason
    return entry


def _snapshot(workdir: Path, rels: list[str]) -> dict[str, bytes | None]:
    """写入前对全部目标文件留底字节（None = 文件不存在，回滚时删除）。"""
    snapshot: dict[str, bytes | None] = {}
    for rel in rels:
        try:
            snapshot[rel] = (workdir / rel).read_bytes()
        except OSError:
            snapshot[rel] = None
    return snapshot


def _restore_snapshot(workdir: Path, snapshot: dict[str, bytes | None]) -> None:
    """尽力回滚到留底状态；单文件失败继续（不影响其余文件回滚）。"""
    for rel, data in snapshot.items():
        target = workdir / rel
        try:
            if data is None:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
        except OSError:
            continue


def _atomic_write(path: Path, data: bytes) -> None:
    """同目录临时文件 + os.replace 原子替换，写失败不留半成品。"""
    tmp = path.with_name(path.name + _TEMP_SUFFIX)
    try:
        with open(tmp, "wb") as sink:
            sink.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------- 主入口


def apply_patches(
    patches: list[Patch],
    workdir: Path | str,
    *,
    yes: bool = False,
    only_index: int | None = None,
    require_all_verified: bool = False,
) -> ApplyResult:
    """把选中的 patches 应用回 workdir 源码（默认 dry-run 预览）。

    Args:
        patches: 报告中的全部 Patch（顺序即 GET /patches 的补丁序号）。
        workdir: 目标源码根目录（补丁路径相对该目录解析）。
        yes: False = dry-run（不落盘）；True = 校验全通过才写入。
        only_index: 只处理第 N 个补丁（0 起）；None = 全部。
        require_all_verified: --all-verified 闸门——选中补丁必须全部 verified。

    Returns:
        ApplyResult。any-rejected 时（yes 模式）整体未落盘（all-or-nothing）。
    """
    root = Path(workdir).resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"目标源码目录不存在或不是目录：{root}")

    selected = [
        (i, p) for i, p in enumerate(patches) if only_index is None or i == only_index
    ]
    result = ApplyResult(dry_run=not yes)

    if require_all_verified:
        bad = [(i, p) for i, p in selected if p.apply_status != "verified"]
        if bad:
            names = "、".join(f"{p.id}（{p.apply_status}）" for _, p in bad)
            for i, p in selected:
                result.rejected.append(
                    _outcome_entry(i, p, [], f"--all-verified 闸门：{names} 状态非 verified，未应用任何补丁")
                )
            return result

    # 规划阶段：逐 patch 解析 diff + 校验指纹 + 计算新内容（mutated 模拟顺序链）。
    # 任何拒绝 → yes 模式整体不落盘；dry-run 照常列出后续补丁的预检结果供预览。
    mutated: dict[str, bytes] = {}
    plans: list[tuple[int, Patch, list[_FilePatch], dict[str, bytes], list[str]]] = []
    for index, patch in selected:
        files, err = _parse_unified_diff(patch.diff)
        if err:
            result.rejected.append(_outcome_entry(index, patch, [], err))
            continue
        # 路径安全复检（生成侧 validate_diff 已把过关，这里对 workdir 再验一遍）
        bad_path = ""
        for fp in files:
            for rel in (fp.old_rel, fp.new_rel):
                if rel is None:
                    continue
                error = _path_error(rel, root)
                if error:
                    bad_path = f"文件路径不合法 {rel!r}: {error}"
                    break
            if bad_path:
                break
        if bad_path:
            result.rejected.append(_outcome_entry(index, patch, [], bad_path))
            continue

        targets = _diff_targets(patch.diff)
        if not patch.target_sha256:
            if yes:
                result.rejected.append(_outcome_entry(index, patch, targets, (
                    "补丁缺少原文指纹（旧版审计产物），无法校验目标文件是否已被改动；"
                    "请重新审计后再应用"
                )))
                continue
            # dry-run 可预览，但明确标注 yes 将被拒绝
            result.rejected.append(_outcome_entry(index, patch, targets, (
                "（预览）补丁缺少原文指纹（旧版审计产物）：--yes 落盘将被拒绝，请重新审计"
            )))
            continue

        planned_files: dict[str, bytes] = {}
        delete_rels: list[str] = []
        reasons: list[str] = []
        for fp in files:
            if fp.new_rel is None and fp.old_rel is not None:
                # 删除型目标（+++ /dev/null）：规划阶段只核对存在性与指纹，不产生写入
                rel = fp.old_rel
                if rel in mutated:
                    data: bytes | None = mutated[rel]
                else:
                    try:
                        data = (root / rel).read_bytes()
                    except OSError:
                        data = None
                expected = patch.target_sha256.get(rel, "")
                if data is None:
                    reasons.append(f"目标文件 {rel} 不存在，无法删除")
                elif (root / rel).is_symlink():
                    reasons.append(f"目标文件 {rel} 是符号链接，拒绝删除（防语义混淆）")
                elif not expected:
                    reasons.append(f"目标文件 {rel} 的原文指纹为空，拒绝删除")
                elif hashlib.sha256(data).hexdigest() != expected:
                    reasons.append(
                        f"目标文件 {rel} 当前内容 sha256 与审计时不一致，拒绝删除以防覆盖"
                    )
                else:
                    delete_rels.append(rel)
                continue
            write_rel = fp.new_rel if fp.new_rel is not None else fp.old_rel
            assert write_rel is not None
            expected = patch.target_sha256.get(write_rel, "")
            data, _rel, err = _plan_single_file(write_rel, fp, expected, root, mutated)
            if data is None:
                reasons.append(err)
            else:
                planned_files[write_rel] = data
        if reasons:
            result.rejected.append(_outcome_entry(index, patch, targets, "；".join(reasons)))
            continue
        mutated.update(planned_files)
        plans.append((index, patch, files, planned_files, delete_rels, targets))

    if not yes or result.rejected:
        # dry-run 或存在拒绝：一律不落盘（all-or-nothing）
        return result

    # 写入阶段：写前再读目标文件核对指纹；全部目标先留底，任一失败整体回滚。
    all_targets = sorted(
        {rel for _, _, _, planned, _, _ in plans for rel in planned}
        | {rel for _, _, _, _, deletes, _ in plans for rel in deletes}
    )
    snapshot = _snapshot(root, all_targets)
    written: list[str] = []
    try:
        for index, patch, _files, planned, deletes, _targets in plans:
            for rel, data in planned.items():
                # 写前再读核对（防规划与写入间隙的目标变化；同文件多补丁链天然成立：
                # 前一补丁写出的内容正是后一补丁指纹对应的原文）
                try:
                    current = (root / rel).read_bytes()
                except OSError:
                    current = None
                expected = patch.target_sha256.get(rel, "")
                if expected and (current is None or hashlib.sha256(current).hexdigest() != expected):
                    raise OSError(
                        f"写入前核对失败：{rel} 内容在应用过程中发生变化（预期指纹 "
                        f"{expected[:12]}…），已整体回滚"
                    )
                if (root / rel).is_symlink():
                    raise OSError(f"写入前发现 {rel} 已变为符号链接，已整体回滚")
                target = root / rel
                if target.parent != root:
                    target.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write(target, data)
                if rel not in written:
                    written.append(rel)
            # 删除型目标（+++ /dev/null）：写前再核对指纹后删除
            for rel in deletes:
                try:
                    current = (root / rel).read_bytes()
                except OSError:
                    current = None
                expected = patch.target_sha256.get(rel, "")
                if expected and (current is None or hashlib.sha256(current).hexdigest() != expected):
                    raise OSError(
                        f"删除前核对失败：{rel} 内容与审计时不一致，已整体回滚"
                    )
                (root / rel).unlink(missing_ok=True)
                if rel not in written:
                    written.append(rel)
            result.applied.append(_outcome_entry(index, patch, _targets))
    except BaseException:
        _restore_snapshot(root, snapshot)
        raise
    result.written_files = written
    return result


# ---------------------------------------------------------------- 审计记录定位


@dataclass
class AuditRecord:
    """按 audit_id 定位到的审计记录：报告 + 原始源路径（可能为空或 zip）。"""

    report: AuditReport
    source_path: str  # 任务记录的原始输入路径（CLI 直跑可能为空串）
    location: str  # 人类可读的产物来源（db 路径或 report.json 路径）


def _load_report_file(path: Path) -> AuditReport | None:
    try:
        return AuditReport.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


def locate_audit(audit_id: str, work_root: Path | str | None = None) -> AuditRecord:
    """按 audit_id 定位审计报告（TaskStore 优先，其次磁盘 report.json 布局）。

    查找顺序：
    1. CODEAUDIT_DB_PATH（或 <work_root>/audits.db）的 TaskStore；
    2. <work_root>/<audit_id>/reports/report.json（server 任务布局）；
    3. <work_root>/reports/report.json（CLI run 默认布局，报告内 audit_id 须匹配）。

    未找到抛 FileNotFoundError。
    """
    from audit.config import AuditConfig
    from audit.taskstore import TaskStore

    root = Path(work_root) if work_root else Path(AuditConfig.from_env().work_root)
    db_path = Path(os.environ.get("CODEAUDIT_DB_PATH") or (root / "audits.db"))
    if db_path.is_file():
        store: TaskStore | None = None
        try:
            store = TaskStore(db_path)
            entry = store.get(audit_id)
            if entry is not None:
                report = store.get_report(audit_id)
                if report is None:
                    raise FileNotFoundError(
                        f"任务 {audit_id} 存在于 {db_path} 但报告未落盘"
                        f"（status={entry['status']}），无法应用补丁"
                    )
                return AuditRecord(report, str(entry.get("source_path") or ""), str(db_path))
        except FileNotFoundError:
            raise
        except Exception:  # noqa: BLE001 —— db 打不开时回退磁盘布局
            pass
        finally:
            if store is not None:
                try:
                    store.close()
                except Exception:  # noqa: BLE001
                    pass
    for candidate in (root / audit_id / "reports" / "report.json", root / "reports" / "report.json"):
        if candidate.is_file():
            report = _load_report_file(candidate)
            if report is not None and report.audit_id == audit_id:
                return AuditRecord(report, "", str(candidate))
    raise FileNotFoundError(
        f"未找到审计 {audit_id} 的报告（查找过 {db_path} 与 {root} 下的报告目录）；"
        "请确认 audit_id 与 --work-root 是否正确"
    )
