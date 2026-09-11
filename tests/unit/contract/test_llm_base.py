"""T1 契约层自测：FakeLLMClient 回放与 utils。"""

import pytest

from audit.llm.base import FakeLLMClient, LLMResponse, ToolCall
from audit.utils import extract_json, make_id, sha256_text, truncate_lines


async def test_fake_llm_script_replay():
    llm = FakeLLMClient(
        [
            {"tool_calls": [{"name": "record_issues", "arguments": {"issues": []}}]},
            LLMResponse(content="done", usage={"prompt_tokens": 10, "completion_tokens": 2}),
        ]
    )
    r1 = await llm.chat([{"role": "user", "content": "hi"}])
    assert r1.has_tool_calls
    assert r1.tool_calls[0].name == "record_issues"
    assert isinstance(r1.tool_calls[0].arguments, dict)

    r2 = await llm.chat([{"role": "user", "content": "hi"}])
    assert r2.content == "done"

    r3 = await llm.chat([{"role": "user", "content": "hi"}])
    assert r3.content == "" and not r3.has_tool_calls  # 耗尽安全默认
    assert llm.usage_totals()["llm_calls"] == 3


async def test_fake_llm_records_calls():
    llm = FakeLLMClient()
    await llm.chat([{"role": "user", "content": "x"}], tools=[{"type": "function"}], json_mode=True)
    assert llm.calls[0]["json_mode"] is True


def test_make_id_and_sha():
    assert make_id("ISS", 42) == "ISS-0042"
    assert len(sha256_text("abc")) == 64


def test_truncate_lines():
    lines = [str(i) for i in range(10)]
    out = truncate_lines(lines, max_lines=3)
    assert len(out) == 4 and "截断" in out[-1]


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('说明文本\n```json\n[1, 2]\n```\n尾注') == [1, 2]
    assert extract_json('前缀 {"b": {"c": 2}} 后缀') == {"b": {"c": 2}}
    with pytest.raises(ValueError):
        extract_json("完全不是 JSON")
