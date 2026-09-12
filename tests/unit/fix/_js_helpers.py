"""tests/unit/fix JS 共享夹具（W7-A3）：JS 缺陷靶项目、修复 diff、node:test 代码。

全部离线：LLM 一律脚本回放；node --test / node --check 是毫秒级本地子进程（禁网）。
所有样例文件统一 LF 写盘，保证 git apply 与 diff 逐字节对齐。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from audit.workspace import WorkspaceContext

# ---------------------------------------------------------------- 样例 JS 项目

# 缺陷靶点：SQL 拼接（规则 JS-SQL-CONCAT，critical；修复前 db.all(query) 无参数）
STORE_JS = """// 用户存储：演示项目的 JS 缺陷靶点（SQL 拼接注入，critical）。
function findUser(db, username) {
    const query = "SELECT * FROM users WHERE name = '" + username + "'";
    return db.all(query);
}

module.exports = { findUser };
"""

# 现有测试（node:test，CommonJS .js）：修复后才可能全绿（拒绝无参数 SQL）
TESTS_STORE_TEST_JS = """// 现有测试：findUser 的正常路径（修复后才可能全绿：拒绝无参数 SQL）。
const { test } = require('node:test');
const assert = require('node:assert');
const { findUser } = require('../store.js');

function makeDb(rows) {
    return {
        all(sql, params) {
            if (!sql.includes('?')) {
                throw new Error('refusing insecure query: ' + sql);
            }
            const wanted = Array.isArray(params) ? params[0] : params;
            return rows.filter((row) => row.name === wanted);
        },
    };
}

test('finds matching user', () => {
    const db = makeDb([{ name: 'alice', role: 'admin' }, { name: 'bob', role: 'viewer' }]);
    assert.deepStrictEqual(findUser(db, 'alice'), [{ name: 'alice', role: 'admin' }]);
});

test('injection payload treated literally', () => {
    const db = makeDb([{ name: 'alice', role: 'admin' }]);
    assert.deepStrictEqual(findUser(db, "x' OR '1'='1"), []);
});
"""

# 修复 diff：SQL 拼接改参数化（? + 参数），与 STORE_JS 第 2-5 行逐字节对齐
JS_STORE_DIFF = (
    "diff --git a/store.js b/store.js\n"
    "--- a/store.js\n"
    "+++ b/store.js\n"
    "@@ -2,4 +2,4 @@\n"
    " function findUser(db, username) {\n"
    "-    const query = \"SELECT * FROM users WHERE name = '\" + username + \"'\";\n"
    "-    return db.all(query);\n"
    '+    const query = "SELECT * FROM users WHERE name = ?";\n'
    "+    return db.all(query, username);\n"
    " }\n"
)


# ---------------------------------------------------------------- 工厂函数


def write_file(path: Path, content: str) -> None:
    """以 UTF-8 + LF 写文件（Windows 下 write_text 默认会把 \\n 翻译成 CRLF）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def make_js_workspace(tmp_path: Path, files: dict[str, str]) -> WorkspaceContext:
    """按 {相对路径: 内容} 造 JS 工作副本，返回指向它的 WorkspaceContext。"""
    src = tmp_path / "src"
    for rel, content in files.items():
        write_file(src / rel, content)
    return WorkspaceContext(
        audit_id="jstests", src_root=src, work_root=tmp_path, db_path=tmp_path / "index.db"
    )


# TestGen 脚本（ScriptedJsLLM 的 testgen 响应）：修复后 findUser 的 node:test
# 回归用例（ESM .test.mjs 语义，真跑全绿）。与 tests/unit/testgen/_js_fixtures.py
# 保持同源（两测试目录 sys.path 隔离，各自持有一份）。
ESM_TESTGEN_CODE = """import { test } from 'node:test';
import assert from 'node:assert';
import { findUser } from '../../store.js';

function makeDb(rows) {
    return {
        all(sql, params) {
            const wanted = Array.isArray(params) ? params[0] : params;
            return rows.filter((row) => row.name === wanted);
        },
    };
}

// kind: normal
test('finds matching user', () => {
    const db = makeDb([{ name: 'alice', role: 'admin' }]);
    assert.deepStrictEqual(findUser(db, 'alice'), [{ name: 'alice', role: 'admin' }]);
});

// kind: boundary
test('unknown user returns empty', () => {
    const db = makeDb([{ name: 'alice', role: 'admin' }]);
    assert.deepStrictEqual(findUser(db, 'nobody'), []);
});

// kind: error
test('injection payload treated literally', () => {
    const db = makeDb([{ name: 'alice', role: 'admin' }]);
    assert.deepStrictEqual(findUser(db, "x' OR '1'='1"), []);
});
"""


def llm_json(diff: str, rationale: str = "最小修复：仅改动问题相关行") -> dict[str, str]:
    """FakeLLM 脚本项：返回 Fix Agent 约定的 JSON 字符串。"""
    return {"content": json.dumps({"diff": diff, "rationale": rationale}, ensure_ascii=False)}


class ScriptedJsLLM:
    """脚本化假 LLM（按 system prompt 关键词分发），供 run_audit 端到端注入。

    接口与 audit.llm.base.LLMClient 兼容（鸭子类型）；fix → JS_STORE_DIFF 的
    JSON，testgen → ESM_TESTGEN_CODE 的 ```javascript 围栏，其余空响应。
    """

    def __init__(self) -> None:
        self.calls: dict[str, int] = {"fix": 0, "testgen": 0, "other": 0}

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        json_mode: bool = False,
        temperature: float = 0.2,
    ) -> Any:
        from audit.llm.base import LLMResponse

        system = next(
            (str(m.get("content", "")) for m in messages if m.get("role") == "system"), ""
        )
        if "修复工程师" in system:
            self.calls["fix"] += 1
            payload = {"diff": JS_STORE_DIFF, "rationale": "把拼接改为占位符 ? + 参数，由驱动层转义"}
            return LLMResponse(content=json.dumps(payload, ensure_ascii=False), model="scripted")
        if "测试工程师" in system:
            self.calls["testgen"] += 1
            fenced = "```javascript\n" + ESM_TESTGEN_CODE + "```"
            return LLMResponse(content=fenced, model="scripted")
        self.calls["other"] += 1
        return LLMResponse(content="", model="scripted")

    def usage_totals(self) -> dict[str, int]:
        return {"llm_calls": sum(self.calls.values()), "prompt_tokens": 0, "completion_tokens": 0}

    async def aclose(self) -> None:
        return None
