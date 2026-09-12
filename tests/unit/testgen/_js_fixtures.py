"""tests/unit/testgen JS 共享夹具（W7-A3）：JS 靶模块与 node:test 样例代码。

与 tests/unit/fix/_js_helpers.py 同源的常量在本目录独立一份（两个测试目录
各自 sys.path 隔离，无法互相导入）；node --test / node --check 是毫秒级本地
子进程（禁网），样例文件统一 LF 写盘。
"""

from __future__ import annotations

from pathlib import Path

from audit.workspace import WorkspaceContext

STORE = "store.js"

# 缺陷靶点模块（CommonJS）：findUser 的 SQL 拼接（修复后为 ? + 参数）
STORE_JS = """// 用户存储：演示项目的 JS 缺陷靶点（SQL 拼接注入，critical）。
function findUser(db, username) {
    const query = "SELECT * FROM users WHERE name = '" + username + "'";
    return db.all(query);
}

module.exports = { findUser };
"""

# 修复后的模块（testgen 面向修复后函数生成回归用例——与端到端流程中 fix 先行一致）
STORE_JS_FIXED = """// 用户存储：演示项目的 JS 缺陷靶点（SQL 拼接注入，critical）。
function findUser(db, username) {
    const query = "SELECT * FROM users WHERE name = ?";
    return db.all(query, username);
}

module.exports = { findUser };
"""

# FakeLLM 返回的合法 node:test 代码（ESM，真跑全绿；含 kind 注释与 3 处 assert）
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

# 静态校验通过、但 node --test 必挂的版本（断言期望值推演错误）
ESM_TESTGEN_BAD_CODE = ESM_TESTGEN_CODE.replace(
    "findUser(db, 'alice'), [{ name: 'alice', role: 'admin' }]);",
    "findUser(db, 'alice'), [{ name: 'alice', role: 'boss' }]);",
    1,
)

# CommonJS 写法：require( 被 .mjs ESM 守卫拒绝（校验层拒绝，不落盘）
ESM_TESTGEN_REQUIRE_CODE = """// kind: normal
const { test } = require('node:test');
const assert = require('node:assert');
const { findUser } = require('../../store.js');

test('ok', () => {
    assert.strictEqual(1 + 1, 2);
});
"""


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
        audit_id="jstestgen", src_root=src, work_root=tmp_path, db_path=tmp_path / "index.db"
    )


def fenced_js(code: str) -> str:
    """把 JS 代码包成 ```javascript 围栏（模拟 LLM 原始输出）。"""
    return f"```javascript\n{code}```"
