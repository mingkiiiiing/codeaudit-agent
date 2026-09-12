"""W7-A1 并行规则引擎测试：行为等价（demo_proj / 抑制注释 / disabled）、
触发条件（文件数阈值 / rule_scan_workers 决议）、spawn 真跑（Windows 本机）与优雅降级。

行为等价铁律（docs/12 §4）：命中集合与串行版完全一致——顺序可以不同但内容等价，
filter_suppressed / rule_errors 记账等后处理仍在主进程。
"""

from __future__ import annotations

import pytest

import audit.detect.engine as engine
from audit.detect.base import RuleContext
from audit.detect.engine import (
    PARALLEL_SCAN_MIN_FILES,
    _run_rules_on_contexts,
    _scan_file_worker,
    build_rule_contexts,
    resolve_scan_workers,
    run_rules,
)
from audit.detect.registry import get_registry


def _fingerprint(hits) -> list[tuple]:
    """命中内容指纹：忽略聚合顺序，字段全比对（含 meta repr，保证内容等价而非仅数量）。"""
    return sorted(
        (h.file, h.line_start, h.line_end, h.rule_id, h.message, h.snippet, repr(h.meta))
        for h in hits
    )


def _synthetic_contexts(n: int) -> list[RuleContext]:
    """n 个内存态 Python RuleContext（不依赖磁盘，专用于触发条件测试）。"""
    src = "def f(a=[]):\n    try:\n        pass\n    except:\n        pass\n    if a == None:\n        pass\n"
    return [
        RuleContext(rel_path=f"pkg{i % 5}/m{i}.py", language="python", source=src, lines=src.splitlines())
        for i in range(n)
    ]


class InlineExecutor:
    """进程池替身：不经 pickle 在进程内直接 map，用于断言触发条件与并发决议。"""

    def __init__(self, workers: int, log: list[str]) -> None:
        self.workers = workers
        self.log = log

    def map(self, fn, iterable, chunksize: int = 1):  # noqa: ARG002 —— 与 ProcessPoolExecutor 同签名
        items = list(iterable)
        self.log.append(f"map:{len(items)}:workers={self.workers}")
        return (fn(p) for p in items)

    def shutdown(self, wait: bool = True) -> None:
        self.log.append("shutdown")


class ExplodingExecutor:
    """map 必崩的进程池替身：验证优雅降级回串行。"""

    def map(self, fn, iterable, chunksize: int = 1):
        raise RuntimeError("simulated pool crash")

    def shutdown(self, wait: bool = True) -> None:
        pass


@pytest.fixture
def parallel_threshold_open(monkeypatch):
    """把并行触发阈值降为 1：让小样本也走真实进程池路径（Windows spawn 真跑）。"""
    monkeypatch.setattr(engine, "PARALLEL_SCAN_MIN_FILES", 1)


@pytest.fixture
def executor_log(monkeypatch):
    """注入 InlineExecutor 假 executor 工厂，并返回调用日志。"""
    log: list[str] = []

    def _factory(workers: int):
        return InlineExecutor(workers, log)

    monkeypatch.setattr(engine, "_create_executor", _factory)
    return log


# ---------------------------------------------------------------- 并发度决议


class TestResolveScanWorkers:
    def test_auto_small_project_serial(self):
        assert resolve_scan_workers(0, PARALLEL_SCAN_MIN_FILES - 1) == 1

    def test_auto_large_project_uses_min4cpu(self):
        expected = min(engine.MAX_SCAN_WORKERS, engine.os.cpu_count() or 1)
        assert resolve_scan_workers(0, PARALLEL_SCAN_MIN_FILES) == expected

    def test_explicit_one_forces_serial(self):
        assert resolve_scan_workers(1, 100_000) == 1

    def test_explicit_workers_below_threshold_serial(self):
        assert resolve_scan_workers(4, 99) == 1

    def test_explicit_workers_respected_above_threshold(self):
        assert resolve_scan_workers(3, PARALLEL_SCAN_MIN_FILES) == 3
        assert resolve_scan_workers(8, PARALLEL_SCAN_MIN_FILES) == 8

    def test_negative_treated_as_auto(self):
        assert resolve_scan_workers(-3, PARALLEL_SCAN_MIN_FILES) == min(
            engine.MAX_SCAN_WORKERS, engine.os.cpu_count() or 1
        )
        assert resolve_scan_workers(-3, 10) == 1


# ---------------------------------------------------------------- worker 函数（单文件等价）


class TestScanFileWorker:
    def test_worker_hits_equal_serial_registry_run(self):
        registry = get_registry()
        src = (
            "def f(a=[]):\n"
            "    try:\n"
            "        pass\n"
            "    except:\n"
            "        pass\n"
            "    if a == None:\n"
            "        pass\n"
        )
        rc = RuleContext(rel_path="m.py", language="python", source=src, lines=src.splitlines())
        # 串行基线：主进程逐规则 check
        serial_hits = []
        for rule in registry.rules_for("python"):
            serial_hits.extend(rule.check(rc))
        # worker 路径：payload 打包 → 进程内调用 worker → dict 还原
        (payload,) = engine._rule_payloads([rc], registry, set())
        hit_dicts, errors = _scan_file_worker(payload)
        assert errors == {}
        worker_hits = [engine.RuleHit.from_dict(d) for d in hit_dicts]
        assert _fingerprint(worker_hits) == _fingerprint(serial_hits)

    def test_worker_respects_disabled_rules(self):
        registry = get_registry()
        src = "try:\n    pass\nexcept:\n    pass\n"
        rc = RuleContext(rel_path="m.py", language="python", source=src, lines=src.splitlines())
        (payload,) = engine._rule_payloads([rc], registry, {"PY-BARE-EXCEPT"})
        hit_dicts, _errors = _scan_file_worker(payload)
        assert all(d["rule_id"] != "PY-BARE-EXCEPT" for d in hit_dicts)

    def test_worker_rebuilds_pyscan_meta_for_python(self):
        registry = get_registry()
        src = "try:\n    pass\nexcept:\n    pass\n"
        rc = RuleContext(rel_path="m.py", language="python", source=src, lines=src.splitlines())
        (payload,) = engine._rule_payloads([rc], registry, set())
        _scan_file_worker(payload)  # 不崩溃即通过：worker 内部重建 meta["pyscan"]


# ---------------------------------------------------------------- 等价性（demo_proj 真跑）


class TestParallelEquivalence:
    def test_demo_proj_parallel_hits_equal_serial(self, pipeline_ctx, parallel_threshold_open):
        registry = get_registry()
        contexts = build_rule_contexts(pipeline_ctx)
        serial = _run_rules_on_contexts(contexts, registry, {}, rule_scan_workers=1)

        extra: dict = {}
        parallel = _run_rules_on_contexts(contexts, registry, extra, rule_scan_workers=2)

        assert _fingerprint(parallel) == _fingerprint(serial)
        assert parallel  # demo_proj 金标保证确有命中，等价断言非空转
        assert extra["rule_scan_parallel"] == {
            "enabled": True,
            "workers": 2,
            "files": len(contexts),
        }

    def test_parallel_equivalence_with_disabled_rules(self, pipeline_ctx, parallel_threshold_open):
        registry = get_registry()
        contexts = build_rule_contexts(pipeline_ctx)
        disabled = {"PY-BARE-EXCEPT", "PY-EQ-NONE", "JS-VAR"}
        serial = _run_rules_on_contexts(
            contexts, registry, {"disabled_rules": disabled}, rule_scan_workers=1
        )
        parallel = _run_rules_on_contexts(
            contexts, registry, {"disabled_rules": disabled}, rule_scan_workers=2
        )
        assert _fingerprint(parallel) == _fingerprint(serial)
        assert all(h.rule_id not in disabled for h in parallel)

    def test_parallel_equivalence_with_inline_suppression(
        self, pipeline_ctx, parallel_threshold_open
    ):
        # 向工作副本（真实拷贝，非硬链接）写抑制注释场景：过滤发生在主进程，两条路径同口径
        supp = (
            "try:\n"
            "    pass\n"
            "except:  # codeaudit: ignore\n"
            "    pass\n"
            "x = None\n"
            "if x == None:  # codeaudit: ignore[PY-EQ-NONE]\n"
            "    pass\n"
            "y = 1\n"
            "if y == None:\n"
            "    pass\n"
        )
        pipeline_ctx.workspace.abs_path("supp.py").write_text(supp, encoding="utf-8")
        registry = get_registry()
        contexts = build_rule_contexts(pipeline_ctx)
        serial = _run_rules_on_contexts(contexts, registry, {}, rule_scan_workers=1)
        parallel = _run_rules_on_contexts(contexts, registry, {}, rule_scan_workers=2)
        assert _fingerprint(parallel) == _fingerprint(serial)
        # 抑制语义本身生效：裸 ignore 行与指定 ID 行无命中，未指定行保留
        assert any(h.rule_id == "PY-EQ-NONE" and h.line_start == 9 for h in parallel)

    def test_rule_errors_recorded_in_main_process(self, pipeline_ctx, parallel_threshold_open):
        # 用自定义坏规则注册表验证：worker 内单规则崩溃被记账、其余规则照常
        from audit.detect.base import Rule as BaseRule
        from audit.models import Category, Severity

        class CrashRule(BaseRule):
            id = "TEST-CRASH"
            category = Category.BUG
            severity = Severity.LOW
            languages = ("python",)
            description = "必崩规则"

            def check(self, ctx):
                raise ValueError("boom")

        reg = engine.RuleRegistry()
        reg.register_all(get_registry().all_rules)
        reg.register(CrashRule())
        contexts = build_rule_contexts(pipeline_ctx)

        serial_extra: dict = {}
        serial = _run_rules_on_contexts(contexts, reg, serial_extra, rule_scan_workers=1)
        parallel_extra: dict = {}
        parallel = _run_rules_on_contexts(contexts, reg, parallel_extra, rule_scan_workers=2)
        assert _fingerprint(parallel) == _fingerprint(serial)
        assert serial_extra["rule_errors"]["TEST-CRASH"] == parallel_extra["rule_errors"]["TEST-CRASH"]
        assert "boom" in parallel_extra["rule_errors"]["TEST-CRASH"]


# ---------------------------------------------------------------- 触发条件与降级


class TestTriggerConditions:
    def test_small_project_serial_by_default(self, pipeline_ctx, executor_log):
        contexts = build_rule_contexts(pipeline_ctx)  # demo_proj 共 9 个源文件 < 100
        hits = _run_rules_on_contexts(contexts, get_registry(), {}, rule_scan_workers=0)
        assert executor_log == []  # 未创建 executor（串行）
        assert hits

    def test_large_project_auto_parallel_with_fake_executor(self, executor_log, monkeypatch):
        monkeypatch.setattr(engine.os, "cpu_count", lambda: 4)  # 稳定并发度决议
        contexts = _synthetic_contexts(120)
        hits = _run_rules_on_contexts(contexts, get_registry(), {}, rule_scan_workers=0)
        assert executor_log == [f"map:120:workers={min(engine.MAX_SCAN_WORKERS, 4)}", "shutdown"]
        serial = _run_rules_on_contexts(contexts, get_registry(), {}, rule_scan_workers=1)
        assert _fingerprint(hits) == _fingerprint(serial)

    def test_explicit_one_forces_serial_even_for_large_sets(self, executor_log):
        contexts = _synthetic_contexts(120)
        _run_rules_on_contexts(contexts, get_registry(), {}, rule_scan_workers=1)
        assert executor_log == []

    def test_explicit_workers_below_threshold_serial(self, executor_log):
        contexts = _synthetic_contexts(PARALLEL_SCAN_MIN_FILES - 1)
        _run_rules_on_contexts(contexts, get_registry(), {}, rule_scan_workers=4)
        assert executor_log == []

    def test_run_rules_wires_config_workers(self, pipeline_ctx, executor_log, parallel_threshold_open):
        pipeline_ctx.config.rule_scan_workers = 2
        run_rules(pipeline_ctx)
        assert executor_log and executor_log[0].startswith("map:")

        executor_log.clear()
        pipeline_ctx.config.rule_scan_workers = 1
        run_rules(pipeline_ctx)
        assert executor_log == []


class TestGracefulDegradation:
    def test_executor_creation_failure_falls_back_to_serial(
        self, pipeline_ctx, parallel_threshold_open, monkeypatch
    ):
        def _boom(workers: int):
            raise OSError("no process pool on this platform")

        monkeypatch.setattr(engine, "_create_executor", _boom)
        contexts = build_rule_contexts(pipeline_ctx)
        serial = _run_rules_on_contexts(contexts, get_registry(), {}, rule_scan_workers=1)

        extra: dict = {}
        parallel = _run_rules_on_contexts(contexts, registry=get_registry(), extra=extra, rule_scan_workers=2)
        assert _fingerprint(parallel) == _fingerprint(serial)  # 回退串行，命中一致
        info = extra["rule_scan_parallel"]
        assert info["enabled"] is False
        assert info["workers"] == 2
        assert "no process pool" in info["reason"]

    def test_map_failure_falls_back_to_serial(self, pipeline_ctx, parallel_threshold_open, monkeypatch):
        monkeypatch.setattr(engine, "_create_executor", lambda workers: ExplodingExecutor())
        contexts = build_rule_contexts(pipeline_ctx)
        serial = _run_rules_on_contexts(contexts, get_registry(), {}, rule_scan_workers=1)

        extra: dict = {}
        parallel = _run_rules_on_contexts(contexts, get_registry(), extra, rule_scan_workers=2)
        assert _fingerprint(parallel) == _fingerprint(serial)
        assert extra["rule_scan_parallel"]["enabled"] is False
        assert "simulated pool crash" in extra["rule_scan_parallel"]["reason"]
