"""L2 金丝雀双版本回放工具（W10-A3）：旧 tag（git worktree）vs 新版（项目根工作区）
双端口自起服务，固定请求集逐项双发、归一化对拍，产出 markdown 对照报告。

用法::

    python -m bench.canary.replay [--old-tag v0.5.0] [--new-ref HEAD]
                                  [--port-base 8915] [--out md路径] [--quick]

流程（全程离线：启动前清除 os.environ 的 GLM_API_KEY / GLM_BASE_URL / GLM_MODEL，
服务子进程继承净化后的环境，走 FakeLLM/纯规则口径）：

  1. ``git worktree add <tmp>/canary_old <old-tag>`` 检出旧版本（失败报错退出 2）；
  2. 旧服务子进程 cwd=worktree 目录（import audit 走旧代码），新版 cwd=项目根
     （以工作区实际状态运行，含未提交改动）；依赖共用当前环境，不做 pip install；
  3. 两服务就绪（轮询 /api/health，60s 超时）后按固定请求集逐项双发：
     - GET /api/health（剥离 version 字段后比较——双版本号必然不同）；
     - GET /api/audits（空表，直接比较）；
     - POST /api/audits/upload 上传 demo/mini_app 内存 zip → 双端各自轮询至终态
       （上限 240s）→ GET report?format=json / md / html、GET issues?limit=1000；
  4. 归一化后逐项 diff：全空 → PASS（退出码 0）；任一差异 → FAIL（退出码 1），
     差异项给出两端归一化后首处分歧摘要（各截 500 字符）写入报告。

归一化口径（两端必然不同的运行期字段属预期差异，必须剥离）：
  - JSON 递归剥离 audit_id / created_at / duration_sec / schema_version；
  - 任务 ID 作为**值**出现时同样归一化：上传件被服务端存为 <audit_id>.zip，
    报告 project_name 即取该文件名 stem，故 project_name 的值就是两端各自的任务 ID
    （md/html 标题行同理）——凡等于任一端任务 ID 的字符串值/子串统一替换为 <audit_id>；
  - md/html 按行剔除含「审计 ID / 生成时间戳样式 / 总耗时」的行后再 diff。

清理（finally 保证）：双端 DELETE 本次创建的任务 → 服务 stop →
``git worktree remove --force`` + 残留临时目录删除。
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MINI_APP = PROJECT_ROOT / "demo" / "mini_app"
RESULTS_DIR = PROJECT_ROOT / "bench" / "results"

DEFAULT_OLD_TAG = "v0.5.0"
DEFAULT_NEW_REF = "HEAD"
DEFAULT_PORT_BASE = 8915

READY_TIMEOUT_SEC = 60.0  # 服务就绪轮询上限
AUDIT_TIMEOUT_SEC = 240.0  # 上传任务轮询至终态上限
POLL_INTERVAL = 0.25  # 轮询间隔（压测脚本惯例口径，非固定 sleep 等任务）
DIFF_EXCERPT_CHARS = 500  # 差异摘要每端截断长度
EXCERPT_CONTEXT_CHARS = 80  # 差异摘要向前携带的上下文长度

# JSON 归一化递归剥离的键（运行期必然不同的字段）
STRIP_JSON_KEYS = frozenset({"audit_id", "created_at", "duration_sec", "schema_version"})
# /api/health 额外剥离 version（双版本号必然不同，属预期差异）
HEALTH_STRIP_KEYS = STRIP_JSON_KEYS | {"version"}

# 离线保证：启动前必须清除的环境变量
GLM_ENV_KEYS = ("GLM_API_KEY", "GLM_BASE_URL", "GLM_MODEL")

# md/html 文本归一化：整行剔除的标记（含审计 ID / 生成时间戳 / 总耗时的行属预期差异）
_VOLATILE_LINE_MARKS = ("审计 ID：", "自动生成（", "总耗时")
_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")

_TERMINAL_STATUSES = ("done", "failed")

# 上传审计成功后的下游对照项（任务级，两端用各自 audit_id 请求）
_DEPENDENT_ROWS = (
    "GET /api/audits/{id}/report?format=json",
    "GET /api/audits/{id}/report?format=md",
    "GET /api/audits/{id}/report?format=html",
    "GET /api/audits/{id}/issues?limit=1000",
)


class WorktreeError(RuntimeError):
    """git worktree 检出失败（主流程按约定转退出码 2）。"""


@dataclass
class CheckRow:
    """单个请求项的双端对照结果（报告对照表一行）。"""

    name: str
    old_status: str
    new_status: str
    ok: bool
    detail: str = ""
    old_excerpt: str = ""
    new_excerpt: str = ""


# ---------------------------------------------------------------- 离线与环境
def scrub_glm_env() -> list[str]:
    """启动前清除 GLM 三键保证全程离线；服务子进程继承净化后的环境。返回被清除的键。"""
    removed = [k for k in GLM_ENV_KEYS if k in os.environ]
    for key in GLM_ENV_KEYS:
        os.environ.pop(key, None)
    return removed


# ---------------------------------------------------------------- 服务子进程
class ServerHandle:
    """自起 uvicorn 服务子进程：cwd 隔离新旧代码，就绪轮询 60s，stop 时 terminate。"""

    def __init__(self, label: str, port: int, cwd: Path, log_path: Path) -> None:
        self.label = label
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        self.cwd = cwd
        self.log_path = log_path
        self.proc: subprocess.Popen[bytes] | None = None
        self._log_file: Any = None

    def start(self) -> None:
        self._log_file = self.log_path.open("wb")
        self.proc = subprocess.Popen(
            [sys.executable, "cli.py", "serve", "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=str(self.cwd),  # Windows 下显式传 str(Path)
            stdout=self._log_file,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + READY_TIMEOUT_SEC
        while time.monotonic() < deadline:
            with contextlib.suppress(Exception):
                if httpx.get(f"{self.base}/api/health", timeout=2.0).status_code == 200:
                    return
            if self.proc.poll() is not None:
                raise RuntimeError(self._diag(f"服务子进程提前退出（exit={self.proc.returncode}）"))
            time.sleep(0.5)
        raise RuntimeError(self._diag(f"服务 {READY_TIMEOUT_SEC:.0f}s 未就绪"))

    def _diag(self, msg: str) -> str:
        tail = ""
        with contextlib.suppress(OSError):
            tail = self.log_path.read_text(encoding="utf-8", errors="replace")[-1500:]
        return f"[canary] {self.label}（端口 {self.port}）{msg}，日志尾部：\n{tail}"

    def stop(self) -> None:
        if self.proc is not None:
            self.proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.proc.wait(timeout=10)
            if self.proc.poll() is None:
                self.proc.kill()
            self.proc = None
        if self._log_file is not None and not self._log_file.closed:
            self._log_file.close()


# ---------------------------------------------------------------- git worktree
def add_worktree(old_tag: str, dest: Path) -> None:
    """``git worktree add`` 检出旧版本到临时目录；失败抛 WorktreeError（退出码 2）。"""
    r = subprocess.run(
        ["git", "worktree", "add", str(dest), old_tag],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if r.returncode != 0 or not dest.is_dir():
        msg = (r.stderr or r.stdout or "").strip()
        raise WorktreeError(f"[canary] git worktree add {old_tag} 失败（exit={r.returncode}）：{msg}")


def remove_worktree(dest: Path) -> None:
    """``git worktree remove --force``；失败兜底 rmtree + prune，保证残留目录被清。"""
    r = subprocess.run(
        ["git", "worktree", "remove", "--force", str(dest)],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if r.returncode != 0:
        print(f"[canary] worktree remove 退出码 {r.returncode}，兜底 rmtree+prune：{(r.stderr or '').strip()[:200]}")
    shutil.rmtree(dest, ignore_errors=True)
    subprocess.run(
        ["git", "worktree", "prune"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


# ---------------------------------------------------------------- 归一化
AUDIT_ID_PLACEHOLDER = "<audit_id>"  # 两端任务 ID 归一化后的统一占位符


def strip_keys(obj: Any, drop: frozenset[str] = STRIP_JSON_KEYS, scrub_ids: tuple[str, ...] = ()) -> Any:
    """递归剥离运行期必然不同的键；任务 ID 作为字符串值出现时（如 project_name）替换为占位符。"""
    if isinstance(obj, dict):
        return {k: strip_keys(v, drop, scrub_ids) for k, v in obj.items() if k not in drop}
    if isinstance(obj, list):
        return [strip_keys(v, drop, scrub_ids) for v in obj]
    if isinstance(obj, str) and scrub_ids and obj in scrub_ids:
        return AUDIT_ID_PLACEHOLDER
    return obj


def normalize_text(text: str, scrub_ids: tuple[str, ...] = ()) -> str:
    """md/html 归一化：任务 ID 子串替换为占位符，再按行剔除含审计 ID / 生成时间戳样式 / 总耗时的行。"""
    for audit_id in scrub_ids:
        if audit_id:
            text = text.replace(audit_id, AUDIT_ID_PLACEHOLDER)
    kept = [
        line
        for line in text.splitlines()
        if not any(mark in line for mark in _VOLATILE_LINE_MARKS) and not _TIMESTAMP_RE.search(line)
    ]
    return "\n".join(kept)


def canonical_json(obj: Any) -> str:
    """归一化对象的稳定序列化（键排序缩进），供首处分歧定位与摘要。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=1)


def first_excerpt(a: str, b: str, limit: int = DIFF_EXCERPT_CHARS) -> tuple[str, str]:
    """两端归一化字符串的首处分歧摘要：自首个差异点向前带少量上下文，各截 limit 字符。"""
    common = min(len(a), len(b))
    idx = common  # 一方是另一方前缀时，分歧点落在较短串末尾
    for i in range(common):
        if a[i] != b[i]:
            idx = i
            break
    start = max(0, idx - EXCERPT_CONTEXT_CHARS)
    end = idx + limit
    return a[start:end], b[start:end]


# ---------------------------------------------------------------- 对照辅助
def _body_json(resp: httpx.Response) -> Any:
    """响应体解析为 JSON；非 JSON（异常页等）包一层 _raw 便于如实对拍。"""
    with contextlib.suppress(Exception):
        return resp.json()
    return {"_raw": resp.text}


def compare_row(
    name: str,
    old_status: int,
    old_body: Any,
    new_status: int,
    new_body: Any,
    strip: frozenset[str] = STRIP_JSON_KEYS,
    scrub_ids: tuple[str, ...] = (),
) -> CheckRow:
    """JSON 类请求对照：状态须双 200，归一化体须全等；差异记录首处分歧摘要。"""
    if old_status != 200 or new_status != 200:
        return CheckRow(name, str(old_status), str(new_status), False, detail=f"非 200 响应（旧 {old_status} / 新 {new_status}）")
    a, b = strip_keys(old_body, strip, scrub_ids), strip_keys(new_body, strip, scrub_ids)
    if a == b:
        return CheckRow(name, "200", "200", True)
    ea, eb = first_excerpt(canonical_json(a), canonical_json(b))
    return CheckRow(name, "200", "200", False, detail="归一化后存在差异", old_excerpt=ea, new_excerpt=eb)


def compare_text_row(
    name: str,
    old_status: int,
    old_text: str,
    new_status: int,
    new_text: str,
    scrub_ids: tuple[str, ...] = (),
) -> CheckRow:
    """md/html 类请求对照：状态须双 200，按行剔除时间戳类行后须全等。"""
    if old_status != 200 or new_status != 200:
        return CheckRow(name, str(old_status), str(new_status), False, detail=f"非 200 响应（旧 {old_status} / 新 {new_status}）")
    a, b = normalize_text(old_text, scrub_ids), normalize_text(new_text, scrub_ids)
    if a == b:
        return CheckRow(name, "200", "200", True)
    ea, eb = first_excerpt(a, b)
    return CheckRow(name, "200", "200", False, detail="按行剔除审计 ID/时间戳/耗时行后存在差异", old_excerpt=ea, new_excerpt=eb)


def _log_row(row: CheckRow) -> None:
    print(f"[canary] {'PASS' if row.ok else 'FAIL'}：{row.name}")


# ---------------------------------------------------------------- 请求集回放
def build_mini_app_zip() -> bytes:
    """demo/mini_app 内存打包（跳过 __pycache），双端上传同一字节流。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(MINI_APP.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                zf.write(p, p.relative_to(MINI_APP).as_posix())
    return buf.getvalue()


def upload_and_wait(client: httpx.Client, zip_payload: bytes) -> tuple[int, str, str, str]:
    """上传 zip 建任务并轮询至终态（≤240s），返回 (HTTP 状态, audit_id, 终态, 错误摘要)。"""
    r = client.post(
        "/api/audits/upload",
        files={"file": ("mini_app.zip", zip_payload, "application/zip")},
        data={"do_fix": "false", "do_tests": "false"},
    )
    if r.status_code != 200:
        return r.status_code, "", f"http-{r.status_code}", r.text[:DIFF_EXCERPT_CHARS]
    audit_id = str(r.json()["audit_id"])
    deadline = time.monotonic() + AUDIT_TIMEOUT_SEC
    while time.monotonic() < deadline:
        rr = client.get(f"/api/audits/{audit_id}")
        if rr.status_code == 200:
            data = rr.json()
            status = str(data.get("status", ""))
            if status in _TERMINAL_STATUSES:
                return 200, audit_id, status, str(data.get("error") or "")
        time.sleep(POLL_INTERVAL)
    return 200, audit_id, "timeout", f"{AUDIT_TIMEOUT_SEC:.0f}s 未到终态"


def run_checks(
    quick: bool,
    client_old: httpx.Client,
    client_new: httpx.Client,
    created: dict[str, list[str]],
) -> list[CheckRow]:
    """固定请求集逐项双发对拍；created 登记双端任务 ID 供 finally 清理。"""
    rows: list[CheckRow] = []

    # 1) 健康检查：剥离 version（双版本号必然不同）后比较
    ro, rn = client_old.get("/api/health"), client_new.get("/api/health")
    row = compare_row("GET /api/health", ro.status_code, _body_json(ro), rn.status_code, _body_json(rn), HEALTH_STRIP_KEYS)
    rows.append(row)
    _log_row(row)

    # 2) 任务列表（此刻空表，直接比较）
    ro, rn = client_old.get("/api/audits"), client_new.get("/api/audits")
    row = compare_row("GET /api/audits（空表）", ro.status_code, _body_json(ro), rn.status_code, _body_json(rn))
    rows.append(row)
    _log_row(row)

    if quick:  # quick 冒烟：只回放读端点两项
        return rows

    # 3) 上传 mini_app zip → 双端各自轮询至终态（两端任务 ID 不同属预期，不入比较）
    payload = build_mini_app_zip()
    so, ao, fin_o, err_o = upload_and_wait(client_old, payload)
    sn, an, fin_n, err_n = upload_and_wait(client_new, payload)
    created["old"].append(ao)
    created["new"].append(an)
    ok = so == 200 and sn == 200 and fin_o == "done" and fin_n == "done"
    detail = "" if ok else (
        f"上传/终态异常：旧 HTTP {so} 终态 {fin_o} {err_o}；新 HTTP {sn} 终态 {fin_n} {err_n}"
        f"（audit_id：旧 {ao} / 新 {an}，两端 ID 不同属预期）"
    )
    row = CheckRow("POST /api/audits/upload（mini_app zip）→ 轮询至终态（≤240s）", f"{so}/{fin_o}", f"{sn}/{fin_n}", ok, detail)
    rows.append(row)
    _log_row(row)

    if not ok:  # 下游对照依赖完成态报告，未就绪则如实标注跳过
        rows.extend(CheckRow(name, "-", "-", False, "上传/轮询未成功，跳过对照") for name in _DEPENDENT_ROWS)
        return rows

    # 任务 ID 归一化集：两端任务 ID 不同属预期，作为键或值出现（上传件 project_name
    # 即服务端存储名 <audit_id>.zip 的 stem）一律替换为占位符后再对拍
    task_ids = (ao, an)

    # 4) 报告 json：递归剥离运行期字段后比较
    rjo = client_old.get(f"/api/audits/{ao}/report", params={"format": "json"})
    rjn = client_new.get(f"/api/audits/{an}/report", params={"format": "json"})
    row = compare_row(_DEPENDENT_ROWS[0], rjo.status_code, _body_json(rjo), rjn.status_code, _body_json(rjn), scrub_ids=task_ids)
    rows.append(row)
    _log_row(row)

    # 5) 报告 md / 6) 报告 html：按行剔除时间戳类行后比较（audit_id 与日期必然不同）
    rmo = client_old.get(f"/api/audits/{ao}/report", params={"format": "md"})
    rmn = client_new.get(f"/api/audits/{an}/report", params={"format": "md"})
    row = compare_text_row(_DEPENDENT_ROWS[1], rmo.status_code, rmo.text, rmn.status_code, rmn.text, scrub_ids=task_ids)
    rows.append(row)
    _log_row(row)

    rho = client_old.get(f"/api/audits/{ao}/report", params={"format": "html"})
    rhn = client_new.get(f"/api/audits/{an}/report", params={"format": "html"})
    row = compare_text_row(_DEPENDENT_ROWS[2], rho.status_code, rho.text, rhn.status_code, rhn.text, scrub_ids=task_ids)
    rows.append(row)
    _log_row(row)

    # 7) 问题列表：语义漂移的主哨兵（规则库变更会在此如实显现）
    rio = client_old.get(f"/api/audits/{ao}/issues", params={"limit": "1000"})
    rin = client_new.get(f"/api/audits/{an}/issues", params={"limit": "1000"})
    row = compare_row(_DEPENDENT_ROWS[3], rio.status_code, _body_json(rio), rin.status_code, _body_json(rin), scrub_ids=task_ids)
    rows.append(row)
    _log_row(row)

    return rows


# ---------------------------------------------------------------- 清理
def cleanup_tasks(client: httpx.Client, audit_ids: list[str]) -> None:
    """DELETE 本次创建的任务（尽力而为，不掩盖主结论）。"""
    for audit_id in audit_ids:
        if not audit_id:
            continue
        with contextlib.suppress(Exception):
            client.delete(f"/api/audits/{audit_id}")


# ---------------------------------------------------------------- 报告
def render_md(
    rows: list[CheckRow],
    old_tag: str,
    new_ref: str,
    port_base: int,
    quick: bool,
    removed_env: list[str],
) -> str:
    """渲染 markdown 回放报告：环境头 / 逐请求对照表 / 差异明细 / 结论行。"""
    failed = [r for r in rows if not r.ok]
    passed = len(rows) - len(failed)
    verdict = "PASS" if not failed else "FAIL"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    mode = "quick 冒烟（仅 health + 任务列表两项）" if quick else "完整回放（读端点 + 上传审计全链路）"
    lines = [
        "# L2 金丝雀双版本回放报告（bench/canary/replay.py，W10-A3）",
        "",
        "## 环境",
        "",
        f"- **生成时间**：{now}",
        f"- **旧版本**：git ref `{old_tag}`（git worktree 检出至临时目录，端口 {port_base}，import audit 走旧代码）",
        f"- **新版**：git ref `{new_ref}`（项目根工作区实际状态运行，含未提交改动，端口 {port_base + 1}）",
        f"- **模式**：{mode}",
        f"- **离线保证**：启动前清除环境变量 GLM_API_KEY / GLM_BASE_URL / GLM_MODEL"
        f"（本次实际清除：{', '.join(removed_env) if removed_env else '无，环境本就未设置'}）",
        "- **依赖**：双端共用当前 Python 环境（不做 pip install）",
        "- **归一化口径**：JSON 递归剥离 `audit_id` / `created_at` / `duration_sec` / `schema_version`"
        "（health 另剥离 `version`）；任务 ID 作为**值**出现时（上传件被存为 `<audit_id>.zip`，"
        "报告 `project_name` 即该文件名 stem）统一替换为 `<audit_id>` 占位符；md/html 按行剔除含"
        "「审计 ID / 生成时间戳 / 总耗时」的行——两端任务 ID（新建即生成）与时间戳/耗时必然不同，"
        "属预期差异，不计入分歧",
        "",
        "## 逐请求对照",
        "",
        "| # | 请求 | 旧版状态 | 新版状态 | 结果 |",
        "|---|---|---|---|---|",
    ]
    for i, r in enumerate(rows, 1):
        lines.append(f"| {i} | `{r.name}` | {r.old_status} | {r.new_status} | {'PASS' if r.ok else 'FAIL'} |")

    lines += ["", "## 差异明细", ""]
    if not failed:
        lines.append("（无差异：全部请求归一化后一致）")
    else:
        for r in failed:
            lines.append(f"### {r.name}")
            lines.append("")
            if r.detail:
                lines += [f"- 说明：{r.detail}", ""]
            if r.old_excerpt or r.new_excerpt:
                lines += [
                    f"**旧版（归一化后首处分歧，≤{DIFF_EXCERPT_CHARS} 字符）**：",
                    "",
                    "```",
                    r.old_excerpt if r.old_excerpt else "（空）",
                    "```",
                    "",
                    f"**新版（归一化后首处分歧，≤{DIFF_EXCERPT_CHARS} 字符）**：",
                    "",
                    "```",
                    r.new_excerpt if r.new_excerpt else "（空）",
                    "```",
                    "",
                ]

    lines += [
        "## 结论",
        "",
        f"**{verdict}** —— {passed}/{len(rows)} 项请求归一化后一致"
        + ("" if not failed else f"；{len(failed)} 项存在真实分歧（预期差异已剥离后仍不同）"),
        "",
        "## 诚实边界",
        "",
        "- 新版服务以项目根**工作区实际状态**运行（含未提交改动），`--new-ref` 仅作报告标注；",
        "- 任务 ID / 生成时间 / 耗时为运行期必然不同的字段，已由归一化剥离；",
        "- 若规则库在双版本间变更，mini_app 的 issues/report diff 会非空——这正是金丝雀要抓的"
        "语义漂移，本报告如实呈现；是否属预期契约演进需人工判读差异明细，工具不做定性；",
        "- quick 模式只回放 health + 任务列表两项，供冒烟，不等价于完整语义对拍。",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- 主流程
def run(args: argparse.Namespace) -> int:
    """回放主流程：worktree → 双服务 → 对拍 → 报告 → 清理（退出码 0/1/2）。"""
    removed_env = scrub_glm_env()
    print(f"[canary] 离线保证：已清除环境变量 {removed_env if removed_env else '（环境本就未设置）'}")
    out = Path(args.out)
    tmp = Path(tempfile.mkdtemp(prefix="codeaudit_canary_"))
    worktree = tmp / "canary_old"
    try:
        try:
            add_worktree(args.old_tag, worktree)
        except WorktreeError as exc:
            print(str(exc))
            print("[canary] worktree 检出失败，按约定以退出码 2 结束")
            return 2
        print(f"[canary] worktree 就绪：{worktree}（ref {args.old_tag}）")

        old_srv = ServerHandle("旧版", args.port_base, worktree, tmp / "old_server.log")
        new_srv = ServerHandle("新版", args.port_base + 1, PROJECT_ROOT, tmp / "new_server.log")
        created: dict[str, list[str]] = {"old": [], "new": []}
        rows: list[CheckRow] = []
        infra_error = ""
        try:
            old_srv.start()
            print(f"[canary] 旧版服务就绪：{old_srv.base}（ref {args.old_tag}，cwd={worktree}）")
            new_srv.start()
            print(f"[canary] 新版服务就绪：{new_srv.base}（ref {args.new_ref}，cwd={PROJECT_ROOT}）")
            with httpx.Client(base_url=old_srv.base, timeout=30.0) as co, httpx.Client(
                base_url=new_srv.base, timeout=30.0
            ) as cn:
                try:
                    rows = run_checks(bool(args.quick), co, cn, created)
                finally:  # 先清双端任务（服务存活期），再停服务
                    cleanup_tasks(co, created["old"])
                    cleanup_tasks(cn, created["new"])
        except Exception as exc:  # noqa: BLE001 —— 基础设施/回放异常兜底，如实写入报告后 FAIL
            infra_error = f"{type(exc).__name__}: {exc}"
            print(f"[canary] 基础设施异常：{infra_error}")
        finally:
            old_srv.stop()
            new_srv.stop()
            remove_worktree(worktree)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if not rows and infra_error:
        rows = [CheckRow("（基础设施）服务就绪/回放", "-", "-", False, infra_error)]
    verdict = "PASS" if rows and all(r.ok for r in rows) else "FAIL"
    md = render_md(rows, args.old_tag, args.new_ref, args.port_base, bool(args.quick), removed_env)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    passed = sum(1 for r in rows if r.ok)
    print(f"[canary] 结论：{verdict}（{passed}/{len(rows)} 项归一化后一致）")
    print(f"[canary] 报告已写入：{out}")
    return 0 if verdict == "PASS" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="L2 金丝雀双版本回放（旧 tag worktree vs 项目根工作区，归一化 diff，全程离线）"
    )
    parser.add_argument("--old-tag", default=DEFAULT_OLD_TAG, help=f"旧版本 git ref（默认 {DEFAULT_OLD_TAG}）")
    parser.add_argument(
        "--new-ref",
        default=DEFAULT_NEW_REF,
        help=f"新版版本标识，仅用于报告标注（默认 {DEFAULT_NEW_REF}；新版服务始终以项目根工作区实际状态运行）",
    )
    parser.add_argument(
        "--port-base",
        type=int,
        default=DEFAULT_PORT_BASE,
        help=f"端口基数：旧版用 port-base，新版用 port-base+1（默认 {DEFAULT_PORT_BASE}）",
    )
    parser.add_argument(
        "--out",
        default=str(RESULTS_DIR / f"canary_{datetime.now().strftime('%Y%m%d')}.md"),
        help="markdown 报告输出路径（默认 bench/results/canary_<date>.md）",
    )
    parser.add_argument("--quick", action="store_true", help="跳过审计类请求，只回放 health + 列表两项（冒烟）")
    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
