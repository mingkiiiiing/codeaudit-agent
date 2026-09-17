"""W16 遗留清偿：OSV 在线查询（opt-in）单测——全部 mock 网络，零真实请求。

覆盖：query_osv 解析 / body 组装 / 失败降级记账 / 开关关闭零调用 /
osv_issues 去重与预算 / 与种子库命中合并去重 / rule id 与 severity 映射。
"""

from __future__ import annotations

import json
import urllib.error
from types import SimpleNamespace

import pytest

from audit.depcheck import osv, scanner
from audit.depcheck.manifest import Dependency, parse_requirements
from audit.models import Severity


# ---------------------------------------------------------------- mock 设施


class _FakeResp:
    """模拟 urlopen 返回的 HTTP 响应（上下文管理器 + status + read）。"""

    def __init__(self, payload: bytes = b"{}", status: int = 200):
        self._payload = payload
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self._payload


class _UrlOpenRecorder:
    """替换 urllib.request.urlopen：按脚本应答并记录全部请求。"""

    def __init__(self, payload: bytes = b"{}", status: int = 200, exc: Exception | None = None):
        self.calls: list = []
        self._payload = payload
        self._status = status
        self._exc = exc

    def __call__(self, req, timeout=None):
        self.calls.append((req, timeout))
        if self._exc is not None:
            raise self._exc
        return _FakeResp(self._payload, self._status)

    @property
    def count(self) -> int:
        return len(self.calls)


def _vuln(cve: str = "CVE-2024-22195", score: str = "5.4", fixed: str = "3.1.3",
          vid: str = "GHSA-test-0001", introduced: str = "0") -> dict:
    """构造一条 OSV vuln 响应（字段形状与真实 API 对齐）。"""
    return {
        "id": vid,
        "aliases": [cve, "GHSA-other-9999"],
        "summary": "xmlattr 过滤器未转义属性键（测试摘要）",
        "severity": [{"type": "CVSS_V3", "score": score}],
        "affected": [
            {
                "package": {"name": "jinja2", "ecosystem": "PyPI"},
                "ranges": [
                    {"type": "ECOSYSTEM", "events": [{"introduced": introduced}, {"fixed": fixed}]}
                ],
            }
        ],
    }


def make_ctx(src_root) -> SimpleNamespace:
    return SimpleNamespace(workspace=SimpleNamespace(src_root=src_root), extra={})


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """隔离环境开关，保证用例间互不污染（本机环境变量不渗透进测试）。"""
    monkeypatch.delenv("CODEAUDIT_OSV_ONLINE", raising=False)
    monkeypatch.delenv("CODEAUDIT_DEPCHECK_OFF", raising=False)
    monkeypatch.delenv("CODEAUDIT_CFGSECRET_OFF", raising=False)


# ---------------------------------------------------------------- query_osv 纯函数


class TestQueryOsv:
    def test_parses_vulns_and_request_body(self, monkeypatch):
        rec = _UrlOpenRecorder(json.dumps({"vulns": [_vuln()]}).encode("utf-8"))
        monkeypatch.setattr("urllib.request.urlopen", rec)
        vulns = osv.query_osv("jinja2", "3.1.2", "pypi")
        assert rec.count == 1
        req, timeout = rec.calls[0]
        assert timeout == osv.DEFAULT_TIMEOUT
        assert req.full_url == osv.OSV_QUERY_URL
        assert req.get_method() == "POST"
        body = json.loads(req.data)
        assert body == {"package": {"name": "jinja2", "ecosystem": "PyPI"}, "version": "3.1.2"}
        assert len(vulns) == 1
        assert vulns[0]["id"] == "GHSA-test-0001"

    def test_empty_version_omits_field(self, monkeypatch):
        rec = _UrlOpenRecorder(b'{"vulns": []}')
        monkeypatch.setattr("urllib.request.urlopen", rec)
        assert osv.query_osv("requests", "", "npm") == []
        body = json.loads(rec.calls[0][0].data)
        assert body["package"]["ecosystem"] == "npm"
        assert "version" not in body  # 区间约束：不带 version，由本地区间过滤

    def test_network_error_degrades_with_error_channel(self, monkeypatch):
        rec = _UrlOpenRecorder(exc=urllib.error.URLError("connection refused"))
        monkeypatch.setattr("urllib.request.urlopen", rec)
        errors: dict[str, str] = {}
        assert osv.query_osv("jinja2", "3.1.2", "pypi", errors=errors) == []
        assert "depcheck.osv" in errors
        assert "connection refused" in errors["depcheck.osv"]

    def test_non_200_degrades_with_error_channel(self, monkeypatch):
        rec = _UrlOpenRecorder(b"server error", status=500)
        monkeypatch.setattr("urllib.request.urlopen", rec)
        errors: dict[str, str] = {}
        assert osv.query_osv("jinja2", "3.1.2", "pypi", errors=errors) == []
        assert "500" in errors["depcheck.osv"]

    def test_bad_json_degrades_with_error_channel(self, monkeypatch):
        rec = _UrlOpenRecorder(b"{not-json")
        monkeypatch.setattr("urllib.request.urlopen", rec)
        errors: dict[str, str] = {}
        assert osv.query_osv("jinja2", "3.1.2", "pypi", errors=errors) == []
        assert "depcheck.osv" in errors  # 解析失败同样记账（repr 含 JSONDecodeError）

    def test_no_vulns_key_returns_empty(self, monkeypatch):
        rec = _UrlOpenRecorder(b'{"ok": true}')
        monkeypatch.setattr("urllib.request.urlopen", rec)
        assert osv.query_osv("jinja2", "3.1.2", "pypi") == []


# ---------------------------------------------------------------- vuln 字段解析


class TestVulnParsing:
    def test_rule_id_prefers_cve_else_osv_id(self):
        assert osv.vuln_rule_id(_vuln(cve="CVE-2024-22195")) == "DEP-CVE-CVE-2024-22195"
        no_cve = _vuln(cve="")
        no_cve["aliases"] = []
        no_cve["id"] = "OSV-2024-12345"
        assert osv.vuln_rule_id(no_cve) == "DEP-OSV-OSV-2024-12345"

    def test_cvss_from_severity_array(self):
        assert osv.vuln_cvss(_vuln(score="9.8")) == pytest.approx(9.8)
        assert osv.vuln_cvss({"severity": [{"type": "CVSS_V3", "score": "bad"}]}) == 0.0
        assert osv.vuln_cvss({}) == 0.0

    def test_intervals_multi_segment_and_open_bounds(self):
        vuln = {
            "affected": [
                {
                    "ranges": [
                        {
                            "type": "ECOSYSTEM",
                            "events": [
                                {"introduced": "3.0.0"},
                                {"fixed": "3.1.0"},
                                {"introduced": "3.2.0"},
                                {"fixed": "3.3.0"},
                            ],
                        }
                    ]
                }
            ]
        }
        ivs = osv.vuln_intervals(vuln)
        assert len(ivs) == 2  # 一条 range 多段 introduced/fixed 拆为多个区间
        # introduced="0" 视为无下界；无 fixed 视为无上界
        open_vuln = {
            "affected": [{"ranges": [{"events": [{"introduced": "0"}]}]}]
        }
        ivs2 = osv.vuln_intervals(open_vuln)
        assert len(ivs2) == 1 and ivs2[0].lo is None and ivs2[0].hi is None


# ---------------------------------------------------------------- osv_issues 组装


def _dep(name: str, ecosystem: str, *specs: str) -> Dependency:
    from audit.depcheck.manifest import parse_constraint_str

    cons = [c for s in specs for c in parse_constraint_str(s)]
    return Dependency(name=name, raw_name=name, ecosystem=ecosystem, constraints=cons)


class TestOsvIssues:
    def test_new_cve_reported_with_source_osv(self):
        vuln = _vuln(cve="CVE-2099-0001", score="9.8")
        deps = [_dep("jinja2", "pypi", "==3.1.2")]
        issues = osv.osv_issues([("requirements.txt", deps)], fetch=lambda *a, **k: [vuln])
        assert len(issues) == 1
        iss = issues[0]
        assert "rule:DEP-CVE-CVE-2099-0001" in iss.evidence
        assert "source:osv" in iss.evidence
        assert iss.severity == Severity.CRITICAL  # CVSS 9.8 → critical
        assert "OSV 在线" in iss.description  # 数据来源注明
        assert iss.file == "requirements.txt"

    def test_local_interval_filter_rejects_non_overlap(self):
        # 漏洞只影响 < 3.0.0，清单钉扎 3.1.2：远端条目不适用
        vuln = _vuln(cve="CVE-2099-0002", fixed="3.0.0", introduced="1.0.0")
        deps = [_dep("jinja2", "pypi", "==3.1.2")]
        issues = osv.osv_issues([("requirements.txt", deps)], fetch=lambda *a, **k: [vuln])
        assert issues == []

    def test_no_cve_vuln_uses_osv_rule_id(self):
        vuln = _vuln(cve="", vid="OSV-2024-12345")
        deps = [_dep("jinja2", "pypi", "==3.1.2")]
        issues = osv.osv_issues([("requirements.txt", deps)], fetch=lambda *a, **k: [vuln])
        assert [e for e in issues[0].evidence if e.startswith("rule:")] == [
            "rule:DEP-OSV-OSV-2024-12345"
        ]

    def test_same_package_version_queried_once_across_manifests(self):
        deps1 = parse_requirements("jinja2==3.1.2\n")
        deps2 = parse_requirements("jinja2==3.1.2\n")  # 另一清单重复声明同版本
        calls: list[tuple[str, str]] = []

        def fetch(package, version, ecosystem, errors=None):
            calls.append((package, version))
            return [_vuln()]

        issues = osv.osv_issues(
            [("a/requirements.txt", deps1), ("b/requirements.txt", deps2)], fetch=fetch
        )
        assert len(calls) == 1  # 同包+版本只查一次
        assert len(issues) == 2  # 但两个清单各自报一份（位置不同）

    def test_range_dep_queries_without_pin_version(self):
        deps = parse_requirements("requests>=2.25.0,<2.31.0\n")
        seen: list[str] = []

        def fetch(package, version, ecosystem, errors=None):
            seen.append(version)
            return []

        osv.osv_issues([("requirements.txt", deps)], fetch=fetch)
        assert seen == [""]  # 区间约束不带精确版本（OSV 返回全量后本地过滤）

    def test_query_budget_stops_and_records(self):
        deps = [_dep(f"pkg{i}", "pypi", "==1.0.0") for i in range(5)]
        calls: list[str] = []

        def fetch(package, version, ecosystem, errors=None):
            calls.append(package)
            return []

        errors: dict[str, str] = {}
        osv.osv_issues(
            [("requirements.txt", deps)], fetch=fetch, errors=errors, max_queries=2
        )
        assert len(calls) == 2  # 到达预算即停
        assert "depcheck.osv_budget" in errors

    def test_seed_dedup_annotates_existing_issue(self):
        # 种子库已报（requirements.txt, CVE-2024-22195）→ OSV 同 CVE 去重并加在线确认标注
        from audit.depcheck import advisory as advisory_mod

        deps = parse_requirements("jinja2==3.1.2\n")
        seed = scanner._cve_issues("requirements.txt", deps, advisory_mod.load_advisories())
        assert any(e == "rule:DEP-CVE-CVE-2024-22195" for i in seed for e in i.evidence)
        issues = osv.osv_issues(
            [("requirements.txt", deps)], existing=seed, fetch=lambda *a, **k: [_vuln()]
        )
        assert issues == []  # 不重复产出
        assert "source:osv" in seed[0].evidence  # 既有 Issue 获得在线确认标注

    def test_dep_without_constraints_skipped(self):
        def fetch(package, version, ecosystem, errors=None):
            raise AssertionError("无约束依赖不应发起查询")

        deps = [_dep("anything", "npm")]  # 无 constraints
        assert osv.osv_issues([("package.json", deps)], fetch=fetch) == []


# ---------------------------------------------------------------- 开关与 scanner 接线


class TestSwitchAndWiring:
    def _project(self, tmp_path):
        (tmp_path / "requirements.txt").write_text(
            "jinja2==3.1.2\nrequests>=2.25.0,<2.31.0\n", encoding="utf-8"
        )
        (tmp_path / "app.py").write_text("import flask\n", encoding="utf-8")
        return tmp_path

    def test_switch_off_makes_zero_network_calls(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CODEAUDIT_OSV_ONLINE", raising=False)  # 默认关
        rec = _UrlOpenRecorder(b'{"vulns": []}')
        monkeypatch.setattr("urllib.request.urlopen", rec)
        self._project(tmp_path)
        issues = scanner.run(make_ctx(tmp_path))
        assert rec.count == 0  # 开关关闭：一次网络调用都没有
        assert all("CVE-2099-0001" not in e for i in issues for e in i.evidence)

    def test_switch_zero_value_makes_zero_network_calls(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEAUDIT_OSV_ONLINE", "0")
        rec = _UrlOpenRecorder(b'{"vulns": []}')
        monkeypatch.setattr("urllib.request.urlopen", rec)
        self._project(tmp_path)
        scanner.run(make_ctx(tmp_path))
        assert rec.count == 0

    def test_switch_on_merges_osv_results(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEAUDIT_OSV_ONLINE", "1")
        # jinja2 查询返回 种子库已有 CVE + 全新 CVE；requests 查询返回空
        payloads = {
            "jinja2": [_vuln(), _vuln(cve="CVE-2099-0001", score="9.8")],
            "requests": [],
        }

        def fake_urlopen(req, timeout=None):
            name = json.loads(req.data)["package"]["name"]
            return _FakeResp(json.dumps({"vulns": payloads[name]}).encode("utf-8"))

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        self._project(tmp_path)
        issues = scanner.run(make_ctx(tmp_path))
        by_rule = {}
        for issue in issues:
            for e in issue.evidence:
                if e.startswith("rule:"):
                    by_rule[e[len("rule:"):]] = issue
        # 种子库命中仍在；OSV 新增命中出现；同 CVE 去重后种子条目获在线标注
        assert "DEP-CVE-CVE-2024-22195" in by_rule
        assert "DEP-CVE-CVE-2099-0001" in by_rule
        assert by_rule["DEP-CVE-CVE-2024-22195"].evidence.count("source:osv") == 1
        assert "OSV 在线" in by_rule["DEP-CVE-CVE-2099-0001"].description

    def test_switch_on_network_failure_degrades_not_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEAUDIT_OSV_ONLINE", "1")
        rec = _UrlOpenRecorder(exc=urllib.error.URLError("no network"))
        monkeypatch.setattr("urllib.request.urlopen", rec)
        self._project(tmp_path)
        ctx = make_ctx(tmp_path)
        issues = scanner.run(ctx)  # 网络故障静默降级，不抛异常
        assert any(e == "rule:DEP-CVE-CVE-2024-22195" for i in issues for e in i.evidence)
        assert "depcheck.osv" in ctx.extra["post_scan_errors"]  # repr 记入错误通道

    def test_depcheck_off_disables_osv_too(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEAUDIT_OSV_ONLINE", "1")
        monkeypatch.setenv("CODEAUDIT_DEPCHECK_OFF", "1")
        rec = _UrlOpenRecorder(b'{"vulns": []}')
        monkeypatch.setattr("urllib.request.urlopen", rec)
        self._project(tmp_path)
        scanner.run(make_ctx(tmp_path))
        assert rec.count == 0  # 依赖扫描总开关同样关掉在线查询
