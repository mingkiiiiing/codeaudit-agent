"""audit/understand/architecture.py 静默吞噬簇证据测试（W15-F 卡F，关联审计 A7）。

本卡只加测试、不改生产代码：固化 architecture.py 全部 6 处
「except 后静默 continue/return/pass」的降级行为——坏输入必须优雅降级
（不抛异常、返回空/部分结果），作为集成人裁决是否开修复卡的证据。

【静默吞噬待记账清单】（行号对应 2026-09-15 工作区版本；经查全文无任何
logging 调用，故本文件没有日志断言项——若后续修复卡补了日志，应同步补日志断言）：
1. architecture.py:63-64   _collect_files       单文件 rel/line_count 异常 → 静默跳过该文件（清单无声丢文件）；
2. architecture.py:80-81   _dependency_blob     依赖清单读文本异常 → 静默跳过（技术栈检测无声丢关键词）；
3. architecture.py:112-113 _symbol_names        索引查询异常 → symbols=[] 静默回退正则扫描；
4. architecture.py:124-125 _symbol_names        正则回退读文本异常 → 静默跳过该文件符号；
5. architecture.py:239-240 _llm_enhance         chat/extract_json 异常 → 静默返回启发式底座（LLM 故障对外不可见）；
6. architecture.py:284-285 build_architecture   进度 emit 异常 → pass（代码注释已声明 best-effort，语义合理）。

处置建议（供集成人参考）：1-5 属「降级 + 应记 debug/warning 日志」；仅 6 可豁免。
"""

from __future__ import annotations

from types import SimpleNamespace

from audit.llm.base import FakeLLMClient
from audit.understand import build_architecture


class _BrokenDepFile:
    """伪装依赖清单：is_file 为真但一读就炸（触发 architecture.py:80-81）。"""

    def __init__(self):
        self.read_attempts = 0

    def is_file(self) -> bool:
        return True

    def read_text(self, *args, **kwargs) -> str:
        self.read_attempts += 1
        raise OSError("模拟依赖清单读取失败")


class _ExplodingIndex:
    """索引全量故障（触发 architecture.py:112-113 的逐文件 except）。"""

    def symbols_for_file(self, rel: str):
        raise RuntimeError("模拟索引查询失败")


class _PartiallyBrokenIndex:
    """只有指定文件查询正常，其余全部抛错（验证 except 后循环继续而非整体中止）。"""

    def __init__(self, healthy_rel: str):
        self._healthy = healthy_rel

    def symbols_for_file(self, rel: str):
        if rel == self._healthy:
            return [SimpleNamespace(kind="class", name="OrderService")]
        raise RuntimeError("模拟索引查询失败")


class _ExplodingLLM(FakeLLMClient):
    """chat 即炸的 LLM 客户端（触发 architecture.py:239-240 的 except: return base）。"""

    async def chat(self, messages, tools=None, json_mode=False, temperature=0.2):
        raise RuntimeError("模拟 LLM 服务故障")


class TestPath1CollectFilesSilentSkip:
    """architecture.py:63-64：单文件元信息失败被静默跳过，其余文件不受牵连。"""

    async def test_broken_file_dropped_without_exception(self, pipeline_ctx, monkeypatch):
        orig_line_count = pipeline_ctx.workspace.line_count

        def flaky_line_count(rel_path):
            if str(rel_path).endswith("app/utils/mathx.py"):
                raise OSError("模拟单文件行数统计失败")
            return orig_line_count(rel_path)

        monkeypatch.setattr(pipeline_ctx.workspace, "line_count", flaky_line_count)
        card = await build_architecture(pipeline_ctx)  # 不应抛异常
        assert pipeline_ctx.architecture is card
        # 同目录健康文件仍在：net.py 的符号照常进入模块摘要
        assert "fetch_json" in card.modules["app/utils"]
        # 被静默跳过文件的符号无声消失（清单丢文件的行为证据）
        assert "accumulate" not in card.modules["app/utils"]
        # 入口识别不受牵连
        assert pipeline_ctx.extra["entry_points"] == ["main.py"]


class TestPath2DependencyBlobSilentSkip:
    """architecture.py:80-81：单个依赖清单读失败被静默跳过，其余清单照常参与技术栈猜测。"""

    async def test_broken_manifest_skipped_readable_one_still_used(self, pipeline_ctx, monkeypatch):
        # demo_proj 副本里补一份可读清单（含 flask）
        (pipeline_ctx.workspace.src_root / "requirements.txt").write_text(
            "flask==2.0.1\n", encoding="utf-8"
        )
        broken = _BrokenDepFile()
        orig_abs_path = pipeline_ctx.workspace.abs_path

        def abs_path_with_broken_manifest(name):
            if str(name) == "pyproject.toml":
                return broken  # 本来不存在，这里伪装成存在但读不了
            return orig_abs_path(name)

        monkeypatch.setattr(pipeline_ctx.workspace, "abs_path", abs_path_with_broken_manifest)
        card = await build_architecture(pipeline_ctx)  # 不应抛异常
        # except: continue 分支确实被走到（读了一次就吞掉）
        assert broken.read_attempts == 1
        # 可读清单的技术栈关键词照常生效
        assert "flask" in card.tech_stack


class TestPath3SymbolIndexSilentFallback:
    """architecture.py:112-113：索引查询异常按文件粒度静默回退。"""

    async def test_total_index_failure_falls_back_to_regex(self, pipeline_ctx, monkeypatch):
        monkeypatch.setattr(pipeline_ctx, "index", _ExplodingIndex(), raising=False)
        card = await build_architecture(pipeline_ctx)  # 不应抛异常
        # 全量索引失败 → 正则回退扫描源码，符号表仍产出
        assert "accumulate" in card.modules["app/utils"]
        assert "get_order_summary" in card.modules["app/services"]

    async def test_partial_index_failure_keeps_healthy_symbols(self, pipeline_ctx, monkeypatch):
        monkeypatch.setattr(
            pipeline_ctx,
            "index",
            _PartiallyBrokenIndex(healthy_rel="app/services/orders.py"),
            raising=False,
        )
        card = await build_architecture(pipeline_ctx)  # 不应抛异常
        # 健康文件的索引符号正常收录
        assert "OrderService" in card.modules["app/services"]
        # 索引命中非空即提前返回：损坏文件不会拖垮整组，也不会混入正则符号
        assert "get_order_summary" not in card.modules["app/services"]
        # 该组索引全炸的 app/utils 静默落到正则回退
        assert "fetch_json" in card.modules["app/utils"]


class TestPath4SymbolRegexFallbackSilentSkip:
    """architecture.py:124-125：无索引时正则回退扫描中，单文件读取失败被静默跳过。"""

    async def test_unreadable_file_symbols_dropped_others_kept(self, pipeline_ctx, monkeypatch):
        # 行数统计先钉成常量，隔离 63-64 路径、确保 net.py 仍进入清单
        monkeypatch.setattr(pipeline_ctx.workspace, "line_count", lambda rel_path: 10)
        orig_read = pipeline_ctx.workspace.read_file_text

        def flaky_read(rel_path):
            if str(rel_path).endswith("app/utils/net.py"):
                raise OSError("模拟符号扫描读文件失败")
            return orig_read(rel_path)

        monkeypatch.setattr(pipeline_ctx.workspace, "read_file_text", flaky_read)
        assert pipeline_ctx.index is None  # 前置：确走正则回退分支
        card = await build_architecture(pipeline_ctx)  # 不应抛异常
        # 健康文件 mathx.py 的符号照常出现在 app/utils 摘要
        assert "accumulate" in card.modules["app/utils"]
        # 读不了的 net.py 符号无声消失（丢符号的行为证据）
        assert "fetch_json" not in card.modules["app/utils"]


class TestPath5LLMEnhanceSilentDegrade:
    """architecture.py:239-240：LLM 故障静默降级为启发式底座，对外零异常。"""

    async def test_llm_crash_degrades_to_heuristic(self, pipeline_ctx):
        assert pipeline_ctx.config.enable_llm_review is True  # 前置：确走增强分支
        pipeline_ctx.llm = _ExplodingLLM()
        card = await build_architecture(pipeline_ctx)  # 不应抛异常
        assert pipeline_ctx.architecture is card
        # 启发式底座完整兜底
        assert "python" in card.tech_stack
        assert card.text
        assert pipeline_ctx.extra["entry_points"] == ["main.py"]


class TestPath6ProgressEmitBestEffort:
    """architecture.py:284-285：进度 emit 失败被 pass 吞掉（注释已声明 best-effort，语义合理）。"""

    async def test_emitter_crash_does_not_kill_stage_result(self, pipeline_ctx):
        async def broken_emitter(event):
            raise RuntimeError("模拟 SSE 连接中断")

        pipeline_ctx.emitter = broken_emitter
        card = await build_architecture(pipeline_ctx)  # 不应抛异常
        # 主产物在 emit 失败前已落位：卡片、入口清单均不受影响
        assert pipeline_ctx.architecture is card
        assert card.text
        assert pipeline_ctx.extra["entry_points"] == ["main.py"]
