"""advisory 单测：版本区间匹配（边界命中/不命中）、种子库完整性与固定 CVE 命中回归。"""

from __future__ import annotations

import re

import pytest

from audit.depcheck import advisory
from audit.depcheck.manifest import Constraint, Dependency
from audit.models import Severity


def make_dep(name: str, spec: list[tuple[str, str]], ecosystem: str = "pypi") -> Dependency:
    return Dependency(
        name=name,
        raw_name=name,
        ecosystem=ecosystem,
        constraints=[Constraint(op=op, version=version, line=1) for op, version in spec],
    )


# ---------------------------------------------------------------- 版本解析与比较


class TestVersionParsing:
    def test_plain_release(self):
        assert advisory.parse_version("3.1.3") == (3, 1, 3)
        assert advisory.parse_version("2015.4.28") == (2015, 4, 28)

    def test_prerelease_stripped_to_release(self):
        # 局限性口径：pre/post 修饰符剥除，只按 release 段比较
        assert advisory.parse_version("3.0.0rc1") == (3, 0, 0)
        assert advisory.parse_version("v2.11.0") == (2, 11, 0)

    def test_compare_padding(self):
        assert advisory.vcmp(advisory.parse_version("2.0"), advisory.parse_version("2.0.0")) == 0
        assert advisory.vcmp(advisory.parse_version("10.2.0"), advisory.parse_version("9.9")) > 0
        assert advisory.vcmp(advisory.parse_version("1.26.16"), advisory.parse_version("1.26.17")) < 0


# ---------------------------------------------------------------- 区间交集


class TestIntervalOverlap:
    def test_exact_pin_inside_advisory_range(self):
        dep = make_dep("jinja2", [("==", "3.1.2")])
        jinja2 = next(a for a in advisory.load_advisories() if a.cve == "CVE-2024-22195")
        assert advisory.intervals_overlap(
            advisory.constraints_to_interval(dep.constraints), advisory.advisory_interval(jinja2)
        )

    def test_exact_pin_after_fixed_not_hit(self):
        dep = make_dep("jinja2", [("==", "3.1.4")])
        jinja2 = next(a for a in advisory.load_advisories() if a.cve == "CVE-2024-22195")
        assert not advisory.intervals_overlap(
            advisory.constraints_to_interval(dep.constraints), advisory.advisory_interval(jinja2)
        )

    def test_range_touches_boundary(self):
        # 依赖 [2.31.0, 3.0) 与漏洞 [2.3.0, 2.31.0)：恰好边界相接（开区间）→ 不相交
        dep_iv = advisory.Interval(advisory.parse_version("2.31.0"), True, advisory.parse_version("3"), False)
        adv_iv = advisory.Interval(advisory.parse_version("2.3.0"), True, advisory.parse_version("2.31.0"), False)
        assert not advisory.intervals_overlap(dep_iv, adv_iv)

    def test_single_point_touch_requires_both_inclusive(self):
        left = advisory.Interval(None, True, advisory.parse_version("2.31.0"), True)   # (-∞, 2.31.0]
        right = advisory.Interval(advisory.parse_version("2.31.0"), True, None, True)  # [2.31.0, +∞)
        assert advisory.intervals_overlap(left, right)
        right_open = advisory.Interval(advisory.parse_version("2.31.0"), True, None, True)
        left_open = advisory.Interval(None, True, advisory.parse_version("2.31.0"), False)
        assert not advisory.intervals_overlap(left_open, right_open)

    def test_unbounded_intervals_overlap(self):
        a = advisory.Interval(None, True, None, True)
        b = advisory.Interval(advisory.parse_version("99.0"), True, None, True)
        assert advisory.intervals_overlap(a, b)


class TestConstraintFold:
    def test_caret_semver(self):
        iv = advisory.constraints_to_interval([Constraint("^", "1.2.3")])
        assert iv.lo == (1, 2, 3) and iv.hi == (2, 0, 0)

    def test_caret_zero_major(self):
        iv = advisory.constraints_to_interval([Constraint("^", "0.2.3")])
        assert iv.hi == (0, 3, 0)

    def test_tilde_semver(self):
        iv = advisory.constraints_to_interval([Constraint("~", "6.3.0")])
        assert iv.hi == (6, 4, 0)

    def test_tilde_equal_pep440(self):
        iv = advisory.constraints_to_interval([Constraint("~=", "3.1.0")])
        assert iv.lo == (3, 1, 0) and iv.hi == (3, 2, 0)

    def test_conflicting_pins_return_none(self):
        assert advisory.constraints_to_interval([Constraint("==", "1.0"), Constraint("==", "2.0")]) is None

    def test_wildcard_pin(self):
        iv = advisory.constraints_to_interval([Constraint("==", "1.2.*")])
        assert iv.lo == (1, 2) and iv.hi == (1, 3)


# ---------------------------------------------------------------- 种子库完整性


class TestAdvisoryDatabase:
    def test_loads_at_least_25_entries(self):
        advs = advisory.load_advisories()
        assert len(advs) >= 25

    def test_ecosystem_split(self):
        advs = advisory.load_advisories()
        ecosystems = {a.ecosystem for a in advs}
        assert ecosystems == {"pypi", "npm"}
        assert sum(1 for a in advs if a.ecosystem == "pypi") >= 10
        assert sum(1 for a in advs if a.ecosystem == "npm") >= 10

    def test_all_fields_present_and_wellformed(self):
        cve_re = re.compile(r"^CVE-\d{4}-\d{4,7}$")
        for a in advisory.load_advisories():
            assert all([a.ecosystem, a.package, a.cve, a.fixed, a.summary, a.reference])
            assert cve_re.match(a.cve), a.cve
            assert 0.0 <= a.cvss <= 10.0
            advisory.parse_version(a.fixed)  # 修复版本必须可解析
            advisory.parse_version(a.introduced)  # 引入版本必须可解析
            assert a.reference.startswith("https://")

    def test_unique_cves(self):
        cves = [a.cve for a in advisory.load_advisories()]
        assert len(cves) == len(set(cves))

    def test_missing_db_returns_empty(self, tmp_path):
        assert advisory.load_advisories(tmp_path / "nope.json") == []


# ---------------------------------------------------------------- 固定 CVE 命中回归


class TestKnownMatches:
    """对已知 CVE 用固定版本断言命中/不命中（防种子库字段错配回归）。"""

    def _match(self, dep: Dependency) -> set[str]:
        return {a.cve for _d, a in advisory.match_advisories([dep], advisory.load_advisories())}

    def test_jinja2_pin_3_1_2_hits(self):
        assert "CVE-2024-22195" in self._match(make_dep("jinja2", [("==", "3.1.2")]))

    def test_jinja2_pin_3_1_4_misses(self):
        assert "CVE-2024-22195" not in self._match(make_dep("jinja2", [("==", "3.1.4")]))

    def test_lodash_caret_hits(self):
        assert "CVE-2021-23337" in self._match(make_dep("lodash", [("^", "4.17.19")], ecosystem="npm"))

    def test_requests_range_hits(self):
        hits = self._match(make_dep("requests", [(">=", "2.25.0"), ("<", "2.31.0")]))
        assert "CVE-2023-32681" in hits

    def test_django_pin_4_2_10_hits(self):
        assert "CVE-2024-27351" in self._match(make_dep("django", [("==", "4.2.10")]))

    def test_tar_tilde_6_1_1_hits(self):
        assert "CVE-2021-32803" in self._match(make_dep("tar", [("~", "6.1.1")], ecosystem="npm"))

    def test_fixed_version_no_match(self):
        hits = self._match(make_dep("requests", [("==", "2.31.0")]))
        assert "CVE-2023-32681" not in hits

    def test_ecosystem_isolation(self):
        # 同名包跨生态不串扰：pypi 的 "tar" 不命中 npm tar 的 CVE（pypi 无 tar 条目时为空）
        hits = self._match(make_dep("lodash", [("==", "4.17.20")], ecosystem="pypi"))
        assert "CVE-2021-23337" not in hits

    def test_no_constraints_never_match(self):
        assert self._match(make_dep("lodash", [])) == set()


# ---------------------------------------------------------------- severity 映射


class TestSeverityFromCvss:
    @pytest.mark.parametrize(
        "score,expected",
        [(9.0, Severity.CRITICAL), (10.0, Severity.CRITICAL), (8.9, Severity.HIGH),
         (7.0, Severity.HIGH), (6.9, Severity.MEDIUM), (4.0, Severity.MEDIUM),
         (3.9, Severity.LOW), (0.0, Severity.LOW)],
    )
    def test_thresholds(self, score, expected):
        assert advisory.severity_from_cvss(score) == expected
