"""W14-A1（Minor-6 / Minor-7）：zip 安全解压强化回归测试。

- Minor-6：中央目录声明的 file_size 可伪造，解压过程必须按实际写出字节复核——
  单文件实际写出超过声明值即中止（IngestError），累计实际写出超上限同样中止；
- Minor-7：zip 成员路径逃逸过滤追加 Windows 保留设备名（CON/PRN/AUX/NUL/
  COM1-9/LPT1-9，含 CON.txt 带扩展名形态、目录形态），此类成员跳过不落盘。
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from audit.errors import IngestError
from audit.ingest import core as ingest_core
from audit.ingest.core import ingest

_KB = 1024


def _write_zip(zip_path: Path, members: dict[str, bytes], *, compress: bool = True) -> None:
    with zipfile.ZipFile(
        zip_path, "w", compression=zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    ) as zf:
        for name, data in members.items():
            zf.writestr(name, data)


def _forge_declared_size(zip_path: Path, member_suffix: str, fake_size: int) -> None:
    """把 zip 中央目录中目标成员的声明未压缩大小改写为 fake_size（模拟恶意 zip）。

    中央目录文件头（PK\\x01\\x02）布局：签名 4B，…，未压缩大小位于 +24（4B LE），
    文件名长度位于 +28，文件名位于 +46。
    """
    data = bytearray(zip_path.read_bytes())
    sig = b"PK\x01\x02"
    pos = 0
    hit = False
    while True:
        pos = data.find(sig, pos)
        if pos < 0:
            break
        name_len = int.from_bytes(data[pos + 28 : pos + 30], "little")
        name = bytes(data[pos + 46 : pos + 46 + name_len]).decode("utf-8", "replace")
        if name.endswith(member_suffix):
            data[pos + 24 : pos + 28] = fake_size.to_bytes(4, "little")
            hit = True
        pos += 4
    assert hit, f"member not found in central directory: {member_suffix}"
    zip_path.write_bytes(bytes(data))


# ---------------------------------------------------------------- Minor-6


class TestZipActualBytesGuard:
    def test_declared_smaller_than_actual_rejected(self, tmp_path):
        """伪造声明（声明 100B / 实际 600KB）：接入必须以 IngestError 拒绝并清理。

        注：CPython zipfile 读取时按声明 file_size 截断输出并在不一致时抛
        BadZipFile（"Bad CRC-32"），因此该场景的实际拦截者因 Python 版本而异
        （标准库或 ingest 自身复核）；两条路径最终都包装为 IngestError，
        错误消息均含成员名。
        """
        blob = b"\x00" * (600 * _KB)  # deflate 后很小，实际内容 600KB
        zip_path = tmp_path / "evil.zip"
        _write_zip(zip_path, {"proj/main.py": b"x = 1\n", "proj/blob.bin": blob})
        _forge_declared_size(zip_path, "blob.bin", 100)

        with pytest.raises(IngestError, match="blob.bin"):
            ingest(zip_path, tmp_path, "minor6a")
        assert not (tmp_path / "minor6a").exists()  # R1-16：失败清理半成品

    def test_single_file_guard_direct(self, tmp_path, monkeypatch):
        """确定性覆盖 ingest 自身的单文件实际写出复核分支。

        zipfile 对伪造声明会先行截断/CRC 拒绝（行为随版本变化），此处用假流
        绕开标准库行为：open(blob.bin) 返回完整 600KB 的假流，声明仅 100B——
        第一块写出前即被 ingest 的"实际 > 声明"检查中止。
        """
        actual_blob = b"\x00" * (600 * _KB)
        zip_path = tmp_path / "evil_direct.zip"
        _write_zip(zip_path, {"proj/main.py": b"x = 1\n", "proj/blob.bin": actual_blob})
        _forge_declared_size(zip_path, "blob.bin", 100)

        real_open = zipfile.ZipFile.open

        def fake_open(self, info, *args, **kwargs):
            if info.filename.endswith("blob.bin"):
                return io.BytesIO(actual_blob)
            return real_open(self, info, *args, **kwargs)

        monkeypatch.setattr(zipfile.ZipFile, "open", fake_open)
        with pytest.raises(IngestError, match="实际解压.*超过声明大小"):
            ingest(zip_path, tmp_path, "minor6d")
        assert not (tmp_path / "minor6d").exists()

    def test_cumulative_guard_prefilter_uses_same_threshold(self, tmp_path):
        """累计上限与前置快筛共用同一阈值：声明累计超限被直接拒绝。

        说明：只要每个成员声明 ≥ 自身实际，"累计实际超限"在数学上蕴含
        "声明累计超限"，必然先被前置快筛拦截（同一上限常量）；因此解压循环
        内的累计实际复核是第三道纵深闸（防未来 zipfile 截断行为回退或过滤
        口径变动），真实数据流下不可单测，由白盒审查保证。
        """
        blob = b"\x00" * (700 * _KB)
        zip_path = tmp_path / "evil2.zip"
        _write_zip(zip_path, {"proj/main.py": b"x = 1\n", "proj/a.bin": blob, "proj/b.bin": blob})

        with pytest.raises(IngestError, match="解压总量.*超过上限"):
            ingest(zip_path, tmp_path, "minor6b", max_zip_bytes=1200 * _KB)
        assert not (tmp_path / "minor6b").exists()

    def test_normal_zip_unaffected(self, tmp_path):
        """正常 zip（声明与实际一致）不受新校验影响。"""
        zip_path = tmp_path / "ok.zip"
        _write_zip(zip_path, {"proj/main.py": b"x = 1\n", "proj/data.bin": b"\x00" * (64 * _KB)})
        ctx = ingest(zip_path, tmp_path, "minor6ok", max_zip_bytes=128 * _KB)
        paths = {m.path for m in ctx.manifests}
        assert paths == {"main.py", "data.bin"}


# ---------------------------------------------------------------- Minor-7


class TestWindowsReservedNames:
    @pytest.mark.parametrize(
        "member",
        [
            "proj/CON.txt",  # 带扩展名形态
            "proj/NUL",
            "proj/com1.py",  # 小写
            "proj/LPT9.ini",
            "proj/CON/inner.py",  # 目录形态
            "proj/AUX",  # 大写
        ],
    )
    def test_reserved_name_members_skipped(self, tmp_path, member):
        zip_path = tmp_path / "reserved.zip"
        _write_zip(zip_path, {"proj/main.py": b"x = 1\n", member: b"evil\n"})
        ctx = ingest(zip_path, tmp_path, "minor7")
        paths = {m.path for m in ctx.manifests}
        assert paths == {"main.py"}, f"保留名成员 {member} 未被过滤"

    def test_reserved_name_is_not_absolute_ambiguity(self, tmp_path):
        """保留名过滤不影响同名的普通子串文件名（如 ICON.txt、SECOND.py）。"""
        zip_path = tmp_path / "normal.zip"
        _write_zip(zip_path, {"proj/ICON.txt": b"ok\n", "proj/SECOND.py": b"x = 1\n"})
        ctx = ingest(zip_path, tmp_path, "minor7b")
        paths = {m.path for m in ctx.manifests}
        assert paths == {"ICON.txt", "SECOND.py"}


# ---------------------------------------------------------------- 净化口径


class TestSafeMemberParts:
    def test_escape_members_rejected(self):
        for name in ("/abs.py", "a/../evil.py", "C:/evil.py", "..\\evil.py"):
            assert ingest_core._safe_member_parts(name) is None, name

    def test_reserved_members_rejected(self):
        for name in ("CON", "con.txt", "dir/NUL", "COM1", "lpt1.txt"):
            assert ingest_core._safe_member_parts(name) is None, name

    def test_normal_members_sanitized(self):
        assert ingest_core._safe_member_parts("proj/main.py") == ["proj", "main.py"]
        # Windows 非法字符按 zipfile.extract 同口径替换为 _
        assert ingest_core._safe_member_parts("proj/a<b>.txt") == ["proj", "a_b_.txt"]
        # 双斜杠 / 当前目录段被折叠
        assert ingest_core._safe_member_parts("proj//./x.py") == ["proj", "x.py"]
