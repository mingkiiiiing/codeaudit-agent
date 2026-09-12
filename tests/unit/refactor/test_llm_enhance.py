"""W7-A2 LLM 增强层自测：FakeLLM 脚本增强 / 脚本耗尽降级 / 解析失败重试后降级。

全部离线（FakeLLMClient 脚本回放），验证 docs/12 §4-2 的「降级安全」约束。
"""

from __future__ import annotations

import json

from audit.llm.base import FakeLLMClient, LLMResponse
from audit.models import RefactorProposal
from audit.refactor.llm import enhance_proposals, parse_enhancement

# ---------------------------------------------------------------- parse_enhancement


def test_parse_valid_payload() -> None:
    payload = {"rationale": "更具体的理由", "steps": ["步骤一", "步骤二"], "benefit": "收益"}
    parsed = parse_enhancement(json.dumps(payload, ensure_ascii=False))
    assert parsed == {"rationale": "更具体的理由", "steps": ["步骤一", "步骤二"], "benefit": "收益"}


def test_parse_tolerates_code_fence_and_prose() -> None:
    text = '好的，以下是方案：\n```json\n{"rationale": "r", "steps": ["s1"], "benefit": "b"}\n```\n希望有帮助'
    parsed = parse_enhancement(text)
    assert parsed is not None
    assert parsed["steps"] == ["s1"]


def test_parse_rejects_bad_shapes() -> None:
    assert parse_enhancement("") is None  # FakeLLM 脚本耗尽的空响应
    assert parse_enhancement(None) is None
    assert parse_enhancement("不是 JSON") is None
    assert parse_enhancement('{"rationale": "r", "steps": "不是数组"}') is None  # steps 非数组
    assert parse_enhancement('{"rationale": "r", "steps": []}') is None  # steps 空
    assert parse_enhancement('["数组顶层"]') is None  # 顶层非对象
    # steps 中非字符串元素被剔除，全被剔则视为非法
    assert parse_enhancement('{"steps": [1, 2]}') is None
    parsed = parse_enhancement('{"steps": ["ok", 3]}')
    assert parsed is not None and parsed["steps"] == ["ok"]


# ---------------------------------------------------------------- enhance_proposals


def _proposal(pid: str, confidence: float) -> RefactorProposal:
    return RefactorProposal(
        id=pid,
        title=f"方案 {pid}",
        target="app/big.py::process_order",
        kind="decompose",
        rationale="启发式理由",
        steps=["启发式步骤一", "启发式步骤二"],
        benefits="启发式收益",
        source="heuristic",
        confidence=confidence,
    )


async def test_enhance_replaces_fields_and_upgrades_source() -> None:
    """脚本返回合法增强 JSON → rationale/steps/benefits 被替换、source 升级。"""
    payload = {
        "rationale": "LLM 深化理由：validate/load/save 三段职责边界清晰",
        "steps": ["抽取 _validate()", "抽取 _persist()", "补充回归用例"],
        "benefit": "LLM 深化收益",
    }
    llm = FakeLLMClient([{"content": json.dumps(payload, ensure_ascii=False)}])
    proposal = _proposal("REF-0001", 0.7)

    stats = await enhance_proposals(_ctx_with(llm), [proposal])

    assert stats == {"targets": 1, "enhanced": 1, "degraded": 0}
    assert proposal.rationale.startswith("LLM 深化理由")
    assert proposal.steps == ["抽取 _validate()", "抽取 _persist()", "补充回归用例"]
    assert proposal.benefits == "LLM 深化收益"
    assert proposal.source == "heuristic+llm"
    # json_mode=True 且 system 角色为重构顾问
    assert llm.calls[0]["json_mode"] is True
    assert any(m.get("role") == "system" and "重构顾问" in str(m.get("content")) for m in llm.calls[0]["messages"])


async def test_enhance_empty_script_degrades_to_heuristic() -> None:
    """FakeLLM 脚本耗尽（空 content）→ 安全降级，启发式原文保留。"""
    llm = FakeLLMClient([])  # 无脚本：chat 返回空 content
    proposal = _proposal("REF-0001", 0.7)
    original = (proposal.rationale, list(proposal.steps), proposal.benefits)

    stats = await enhance_proposals(_ctx_with(llm), [proposal])

    assert stats == {"targets": 1, "enhanced": 0, "degraded": 1}
    assert (proposal.rationale, proposal.steps, proposal.benefits) == original
    assert proposal.source == "heuristic"


async def test_enhance_invalid_json_retries_once_then_degrades() -> None:
    """解析失败重试 1 次（第二次请求带纠错提示），仍失败降级。"""
    llm = FakeLLMClient(
        [
            LLMResponse(content="我觉得这个方案不错，但不给 JSON"),
            LLMResponse(content="还是不给你 JSON"),
        ]
    )
    proposal = _proposal("REF-0001", 0.7)

    stats = await enhance_proposals(_ctx_with(llm), [proposal])

    assert stats["degraded"] == 1 and stats["enhanced"] == 0
    assert len(llm.calls) == 2  # 首次 + 重试 1 次
    assert proposal.source == "heuristic"
    assert proposal.steps == ["启发式步骤一", "启发式步骤二"]  # 原文保留


async def test_enhance_retry_then_success() -> None:
    """首次输出不可解析、重试输出合法 → 增强成功。"""
    payload = {"rationale": "重试后的理由", "steps": ["s1"], "benefit": "重试后的收益"}
    llm = FakeLLMClient(
        [
            LLMResponse(content="garbage"),
            LLMResponse(content=json.dumps(payload, ensure_ascii=False)),
        ]
    )
    proposal = _proposal("REF-0001", 0.7)

    stats = await enhance_proposals(_ctx_with(llm), [proposal])

    assert stats["enhanced"] == 1
    assert proposal.rationale == "重试后的理由"
    assert proposal.source == "heuristic+llm"


async def test_enhance_top_n_by_confidence() -> None:
    """只增强置信度最高的 top_n 个（默认 ≤5），其余保持启发式原文。"""
    payloads = [
        {"rationale": f"增强 {i}", "steps": ["s"], "benefit": "b"} for i in range(2)
    ]
    llm = FakeLLMClient([{"content": json.dumps(p, ensure_ascii=False)} for p in payloads])
    proposals = [
        _proposal("REF-0001", 0.60),
        _proposal("REF-0002", 0.80),  # top 2
        _proposal("REF-0003", 0.65),  # top 2（与 0.80 一起入选）
    ]

    stats = await enhance_proposals(_ctx_with(llm), proposals, top_n=2)

    assert stats == {"targets": 2, "enhanced": 2, "degraded": 0}
    assert len(llm.calls) == 2
    enhanced = [p for p in proposals if p.source == "heuristic+llm"]
    assert {p.id for p in enhanced} == {"REF-0002", "REF-0003"}
    untouched = next(p for p in proposals if p.id == "REF-0001")
    assert untouched.rationale == "启发式理由"


async def test_enhance_keeps_heuristic_fields_when_llm_field_empty() -> None:
    """LLM 返回缺 rationale/benefit（空串）时保留启发式对应字段，steps 仍替换。"""
    payload = {"rationale": "", "steps": ["LLM 步骤"], "benefit": ""}
    llm = FakeLLMClient([{"content": json.dumps(payload, ensure_ascii=False)}])
    proposal = _proposal("REF-0001", 0.7)

    stats = await enhance_proposals(_ctx_with(llm), [proposal])

    assert stats["enhanced"] == 1
    assert proposal.rationale == "启发式理由"  # 空串 → 保留原值
    assert proposal.benefits == "启发式收益"
    assert proposal.steps == ["LLM 步骤"]
    assert proposal.source == "heuristic+llm"


# ---------------------------------------------------------------- 夹具

from audit.config import AuditConfig  # noqa: E402
from audit.pipeline import PipelineContext  # noqa: E402


def _ctx_with(llm: FakeLLMClient) -> PipelineContext:
    """最小 ctx（enhance_proposals 只依赖 ctx.llm）。"""
    from audit.workspace import WorkspaceContext

    ws = WorkspaceContext(audit_id="x", src_root=".", work_root=".", db_path="i.db")
    return PipelineContext(
        config=AuditConfig(source_path=".", enable_llm_review=True, api_key="fake"),
        workspace=ws,
        llm=llm,
        emitter=None,  # type: ignore[arg-type] —— 增强层不发事件
    )
