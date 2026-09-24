"""W28-B POST /api/rename server 端点自测：dry-run/apply 两态、治理链（鉴权跟随 /
SOURCE_ROOTS 白名单）、plan 拒绝面（400）与 apply 复检失败（409）。

conftest 的 autouse ``store`` fixture 已注入 tmp 库（本端点不触 store，注入只为
目录内用例间零串扰）；语料用 tests/unit/refactor/fixtures/rename_dogfood/ 拷贝到
tmp_path 后**真调** plan_rename/apply_rename（经 HTTP 端点，不 mock 业务逻辑）。
唯一注入点：并发修改用例在 plan 与 apply 之间包一层真 plan_rename 的薄壳、在
返回前对文件系统做一次真实写入以模拟外部并发改动（apply_rename 保持原实现）。
全程离线零 LLM；治理三键先清空防 shell/.env 残留串扰（形态对齐
test_server_security_w15.py 的 clean_env）。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server import app as server_app

_DOGFOOD = Path(__file__).resolve().parents[1] / "refactor" / "fixtures" / "rename_dogfood"

# 治理三键（与 test_server_security_w15.py 同款）：防用户 shell / .env 残留串扰
_TOKEN_ENVS = ("CODEAUDIT_API_TOKEN", "CODEAUDIT_SOURCE_ROOTS", "CODEAUDIT_RATE_LIMIT")


@pytest.fixture
def clean_env(monkeypatch):
    for name in _TOKEN_ENVS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def client(clean_env) -> TestClient:
    return TestClient(server_app.create_app())


@pytest.fixture
def proj(tmp_path) -> Path:
    """dogfood 语料的独立临时副本（每用例一份，互不污染、不动语料本体）。"""
    dest = tmp_path / "proj"
    shutil.copytree(_DOGFOOD, dest)
    return dest


def _snapshot(root: Path) -> dict[Path, bytes]:
    return {p: p.read_bytes() for p in sorted(root.rglob("*.py"))}


def _rename_body(source: Path, **overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "source_path": str(source),
        "old_name": "calc_total",
        "new_name": "calc_sum",
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------- ① dry-run：200 不落盘 + diff


def test_dry_run_200_preview_without_writing(client, proj):
    """apply 缺省（false）= dry-run：200、applied=false、diff 齐全、磁盘逐字节原样。"""
    before = _snapshot(proj)
    resp = client.post("/api/rename", json=_rename_body(proj))
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["applied"] is False
    assert data["errors"] == []
    # calc_total：service.py（import + 调用 2 点）+ util.py（定义 1 点），dogfood 实测口径
    assert set(data["files"]) == {"service.py", "util.py"}
    assert data["replace_points"] == 3
    assert set(data["diffs"]) == {"service.py", "util.py"}
    assert "-def calc_total" in data["diffs"]["util.py"]
    assert "+def calc_sum" in data["diffs"]["util.py"]
    assert "-from util import MAX_RETRY, build_query, calc_total" in data["diffs"]["service.py"]
    assert "+from util import MAX_RETRY, build_query, calc_sum" in data["diffs"]["service.py"]
    for p, content in before.items():  # dry-run 未落盘：逐文件字节级原样
        assert p.read_bytes() == content, f"dry-run 改写了 {p.name}"


# ---------------------------------------------------------------- ② apply=true：200 落盘 + 内容变更


def test_apply_true_200_writes_files(client, proj):
    resp = client.post("/api/rename", json=_rename_body(proj, apply=True))
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["applied"] is True
    assert data["errors"] == []
    assert set(data["files"]) == {"service.py", "util.py"}
    util_text = (proj / "util.py").read_text(encoding="utf-8")
    assert "def calc_sum(items):" in util_text  # 定义点已改
    assert "calc_total 汇总金额" in util_text  # docstring 陷阱：旧名字面保留（token 级不越界）
    service_text = (proj / "service.py").read_text(encoding="utf-8")
    assert "from util import MAX_RETRY, build_query, calc_sum, format_label, join_names" in service_text
    assert "calc_sum([line.net_amount()" in service_text  # 调用点已改
    assert "build_query, calc_total" not in service_text  # 导入项旧名零残留
    assert "total = calc_total(" not in service_text  # 调用点旧名零残留
    # docstring 陷阱（「跨文件调用 UserRecord.to_display / calc_total。」）按设计保留旧名字面
    assert "calc_total" in (proj / "util.py").read_text(encoding="utf-8")  # util docstring 陷阱同上


# ---------------------------------------------------------------- ③ 冒烟：同副本 dry-run→apply 两连


def test_smoke_dry_run_then_apply_two_calls_fs_consistent(client, proj):
    """冒烟：同一副本先 dry-run 再 apply 两连请求，文件系统实际状态与响应一致。"""
    before = _snapshot(proj)
    dry = client.post("/api/rename", json=_rename_body(proj))
    assert dry.status_code == 200 and dry.json()["applied"] is False
    for p, content in before.items():
        assert p.read_bytes() == content  # 第一步落点：磁盘未动
    applied = client.post("/api/rename", json=_rename_body(proj, apply=True))
    assert applied.status_code == 200
    data = applied.json()
    assert data["applied"] is True
    changed = {p.relative_to(proj).as_posix() for p, c in before.items() if p.read_bytes() != c}
    assert changed == set(data["files"])  # 第二步落点：实际变更文件集合 == 响应 files
    for rel in changed:
        new_text = (proj / rel).read_text(encoding="utf-8")
        assert "calc_sum" in new_text
        assert data["diffs"][rel]  # 响应 diff 与实际落盘一一对应（非空）
    # 二次 plan：旧名定义已消失（幂等口径，dogfood calc_total → calc_sum）
    replan = client.post("/api/rename", json=_rename_body(proj))
    assert replan.status_code == 400
    assert "未找到" in replan.json()["detail"]


# ---------------------------------------------------------------- ④ 越界：SOURCE_ROOTS 白名单 400 不落盘


def test_outside_source_roots_400_no_write(client, monkeypatch, tmp_path, proj):
    """CODEAUDIT_SOURCE_ROOTS 指向无关根：越界 400（即使 apply=true），零落盘。"""
    roots = tmp_path / "roots"
    roots.mkdir()  # 与 proj（tmp_path/proj）无包含关系的无关根
    monkeypatch.setenv("CODEAUDIT_SOURCE_ROOTS", str(roots))
    before = _snapshot(proj)
    resp = client.post("/api/rename", json=_rename_body(proj, apply=True))
    assert resp.status_code == 400
    assert "不在允许的根目录内" in resp.json()["detail"]
    for p, content in before.items():
        assert p.read_bytes() == content  # 治理拒绝路径绝不落盘


# ---------------------------------------------------------------- ⑤ 多定义点：400 不落盘


def test_multiple_definitions_400_no_write(client, proj):
    """dogfood 的 Validator（models.py + validation.py 各一处）：多定义点 400，apply=true 也不落盘。"""
    before = _snapshot(proj)
    resp = client.post(
        "/api/rename",
        json=_rename_body(proj, old_name="Validator", new_name="Checker", apply=True),
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "2 个同名定义点" in detail
    assert "models.py" in detail and "validation.py" in detail  # 中文 detail 附逐定义点位置
    assert "宁拒不改" in detail
    for p, content in before.items():
        assert p.read_bytes() == content  # 计划阶段拒绝：零落盘


# ---------------------------------------------------------------- ⑥ source 不存在：400


def test_source_not_exists_400(client, tmp_path):
    resp = client.post(
        "/api/rename",
        json=_rename_body(tmp_path / "no_such_dir_w28b"),
    )
    assert resp.status_code == 400
    assert "不存在" in resp.json()["detail"]


# ---------------------------------------------------------------- ⑦ apply 复检失败（并发修改）：409 零落盘


def test_apply_conflict_409_when_file_modified_after_plan(client, monkeypatch, proj):
    """apply=true 且 plan 后文件被外部并发修改：409 + 逐文件原因，all-or-nothing 零落盘。

    模拟方式（唯一注入点）：monkeypatch server.app 的 plan_rename 名字为薄壳——
    内部先调**真** plan_rename，返回前对 util.py 做一次真实文件系统追加写入，
    模拟 plan 与 apply 之间的外部并发改动；apply_rename 保持原实现不 mock，
    由其落盘前复检（内容与计划时不一致）真实触发 409 分支。
    """
    real_plan_rename = server_app.plan_rename

    def plan_then_external_write(source_path, old_name, new_name, **kwargs):
        plan = real_plan_rename(source_path, old_name, new_name, **kwargs)
        target = Path(str(source_path)) / "util.py"
        with target.open("ab") as sink:  # 二进制追加：不做换行翻译，快照对账逐字节可控
            sink.write("\n# 外部并发追加\n".encode("utf-8"))
        return plan

    monkeypatch.setattr(server_app, "plan_rename", plan_then_external_write)
    before = _snapshot(proj)
    resp = client.post("/api/rename", json=_rename_body(proj, apply=True))
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "util.py" in detail and "并发修改" in detail  # 逐文件原因
    after = _snapshot(proj)
    assert after[proj / "service.py"] == before[proj / "service.py"]  # 未被外部改动的文件零写入
    expected_util = before[proj / "util.py"] + "\n# 外部并发追加\n".encode("utf-8")
    assert after[proj / "util.py"] == expected_util  # 除外部追加那一笔外零变化（重命名未落盘）
    assert "def calc_total(items):" in (proj / "util.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------- ⑧ 非法名：400 不落盘


def test_invalid_new_name_400_no_write(client, proj):
    before = _snapshot(proj)
    resp = client.post(
        "/api/rename", json=_rename_body(proj, new_name="not-an-ident", apply=True)
    )
    assert resp.status_code == 400
    assert "new_name 不是合法 python 标识符" in resp.json()["detail"]
    for p, content in before.items():
        assert p.read_bytes() == content


# ---------------------------------------------------------------- ⑨ 鉴权跟随 /api/* 中间件


def test_auth_follows_api_middleware(client, monkeypatch, proj):
    """CODEAUDIT_API_TOKEN 非空：无凭据 401；带 X-API-Token 通过后正常 dry-run 200。"""
    monkeypatch.setenv("CODEAUDIT_API_TOKEN", "w28-rename-token")
    denied = client.post("/api/rename", json=_rename_body(proj))
    assert denied.status_code == 401
    assert "未授权" in denied.json()["detail"]
    ok = client.post(
        "/api/rename", json=_rename_body(proj), headers={"X-API-Token": "w28-rename-token"}
    )
    assert ok.status_code == 200  # 凭据正确 → 穿过中间件进入业务（dry-run 200）
    assert ok.json()["applied"] is False


# ---------------------------------------------------------------- ⑩ language 缺省：回归 dry-run 等价（W29-C）


def test_language_default_regression_dry_run_equivalent(client, proj):
    """language 缺省（不传）：与既有 dry-run 用例行为等价（补 language 参数后的回归锚点）。"""
    before = _snapshot(proj)
    resp = client.post("/api/rename", json=_rename_body(proj))
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["applied"] is False
    assert data["errors"] == []
    assert set(data["files"]) == {"service.py", "util.py"}
    assert data["replace_points"] == 3
    assert "-def calc_total" in data["diffs"]["util.py"]
    assert "+def calc_sum" in data["diffs"]["util.py"]
    for p, content in before.items():  # 缺省零变化：dry-run 未落盘
        assert p.read_bytes() == content


# ---------------------------------------------------------------- ⑪ language 非法：plan 校验拒绝 400 零落盘（W29-C）


def test_unsupported_language_go_400_no_write(client, proj):
    """language="go"：plan_rename 校验拒绝（plan.ok=False）→ 400 中文 detail，端点零新增分支。"""
    before = _snapshot(proj)
    resp = client.post("/api/rename", json=_rename_body(proj, language="go"))
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "暂不支持" in detail
    assert "python" in detail
    for p, content in before.items():  # 计划阶段拒绝：零落盘
        assert p.read_bytes() == content


# ---------------------------------------------------------------- ⑫ language 显式 python：透传成功（W29-C）


def test_explicit_language_python_200_passthrough(client, proj):
    """显式 language="python"：透传 plan_rename，dry-run 200 且 diff 正常。"""
    resp = client.post("/api/rename", json=_rename_body(proj, language="python"))
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["applied"] is False
    assert data["errors"] == []
    assert set(data["files"]) == {"service.py", "util.py"}
    assert data["replace_points"] == 3
    assert "-def calc_total" in data["diffs"]["util.py"]
    assert "+def calc_sum" in data["diffs"]["util.py"]
