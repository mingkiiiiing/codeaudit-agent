"""端到端测试：tmp_path 造 mini JS/TS 项目 → build_rule_contexts + run_rules。

验证 JS/TS 规则经默认注册表（registry.py）接入检测引擎后，
能按语言正确路由并在精确行号上命中。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from audit.config import AuditConfig
from audit.detect.engine import build_rule_contexts, run_rules
from audit.llm.base import FakeLLMClient
from audit.pipeline import PipelineContext
from audit.workspace import WorkspaceContext

INDEX_JS = """const APP_NAME = "demo";
var legacy = 1;
if (legacy == 1) {
  console.log("legacy path");
}

function badSql(id) {
  return db.query("SELECT * FROM users WHERE id = " + id);
}

debugger;
setTimeout("cleanup()", 500);
eval(userInput);
const apiKey = "sk-abcdefghijklmnopqrstuvwxyz012345";
fetch("/api/data");
// TODO: remove debug entries
module.exports = { badSql };
"""

RENDER_JS = """export function render(user) {
  const el = document.getElementById("app");
  el.innerHTML = "<b>" + user.name + "</b>";
  document.write(el.outerHTML);
  return el;
}
"""

APP_TS = """import { User } from "./types";

export function load(raw: any): any {
  // @ts-ignore
  const data = raw as any;
  return data;
}

export function parse(input: string) {
  const u = input as unknown as User;
  const a = u!.profile!;
  const b = a!.id!.name;
  const c = b!.trim()!.length;
  const d = c!.toString();
  const e = d!.toLowerCase();
  return e;
}
"""


@pytest.fixture
def js_project(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    (src / "index.js").write_text(INDEX_JS, encoding="utf-8")
    (src / "render.js").write_text(RENDER_JS, encoding="utf-8")
    (src / "app.ts").write_text(APP_TS, encoding="utf-8")
    return src


@pytest.fixture
def js_ctx(js_project: Path, tmp_path: Path) -> PipelineContext:
    async def _noop_emitter(event: dict) -> None:
        return None

    workspace = WorkspaceContext(
        audit_id="js000001",
        src_root=js_project,
        work_root=tmp_path,
        db_path=tmp_path / "index.db",
    )
    config = AuditConfig(source_path=str(js_project), enable_llm_review=False)
    return PipelineContext(
        config=config,
        workspace=workspace,
        llm=FakeLLMClient(),
        emitter=_noop_emitter,
    )


class TestJsTsEndToEnd:
    def test_contexts_routed_by_language(self, js_ctx):
        contexts = {rc.rel_path: rc for rc in build_rule_contexts(js_ctx)}
        assert set(contexts) == {"index.js", "render.js", "app.ts"}
        assert contexts["index.js"].language == "javascript"
        assert contexts["render.js"].language == "javascript"
        assert contexts["app.ts"].language == "typescript"

    def test_expected_hits_by_file_and_line(self, js_ctx):
        hits = run_rules(js_ctx)
        index: dict[tuple[str, str], list[int]] = {}
        for h in hits:
            index.setdefault((h.file, h.rule_id), []).append(h.line_start)
        expected = [
            # index.js：10 个可指认缺陷
            ("index.js", "JS-VAR", [2]),
            ("index.js", "JS-EQEQEQ", [3]),
            ("index.js", "JS-CONSOLE-LOG", [4]),
            ("index.js", "JS-SQL-CONCAT", [8]),
            ("index.js", "JS-DEBUGGER", [11]),
            ("index.js", "JS-SETTIMEOUT-STRING", [12]),
            ("index.js", "JS-EVAL-EXEC", [13]),
            ("index.js", "JS-HARDCODED-SECRET", [14]),
            ("index.js", "JS-FETCH-NO-TIMEOUT", [15]),
            ("index.js", "JS-TODO-FIXME", [16]),
            # render.js
            ("render.js", "JS-INNERHTML", [3]),
            ("render.js", "JS-DOCUMENT-WRITE", [4]),
            # app.ts
            ("app.ts", "TS-IGNORE", [4]),
            ("app.ts", "TS-EXPLICIT-ANY-PARAM", [3]),
            ("app.ts", "TS-FUNC-STYLE", [9]),
            ("app.ts", "TS-NEVER-ASSERT", [10]),
            ("app.ts", "TS-NONNULL-ABUSE", [11]),
        ]
        for file, rule_id, lines in expected:
            assert index.get((file, rule_id)), f"缺少命中: {rule_id} @ {file}"
            assert all(ln in index[(file, rule_id)] for ln in lines), (
                f"{rule_id} @ {file} 行号不符: 期望含 {lines}，实际 {index[(file, rule_id)]}"
            )

    def test_ts_any_hits_both_lines(self, js_ctx):
        hits = run_rules(js_ctx)
        any_lines = {h.line_start for h in hits if h.file == "app.ts" and h.rule_id == "TS-ANY"}
        assert any_lines == {3, 5}

    def test_defect_coverage_at_least_six(self, js_ctx):
        hits = run_rules(js_ctx)
        hit_rule_ids = {h.rule_id for h in hits}
        assert len(hit_rule_ids) >= 15

    def test_line_numbers_within_bounds(self, js_ctx):
        hits = run_rules(js_ctx)
        contexts = {rc.rel_path: len(rc.lines) for rc in build_rule_contexts(js_ctx)}
        assert hits
        for h in hits:
            assert 1 <= h.line_start <= contexts[h.file], f"{h.rule_id} 行号越界: {h.file}:{h.line_start}"
            assert h.line_end >= h.line_start

    def test_no_cross_language_noise(self, js_ctx):
        # TS 专属规则不应在 JS 文件上命中；bug/style 类 JS 规则不应在 TS 文件上命中
        hits = run_rules(js_ctx)
        for h in hits:
            if h.rule_id.startswith("TS-"):
                assert h.file == "app.ts"
            if h.rule_id in {"JS-VAR", "JS-DEBUGGER", "JS-EQEQEQ", "JS-CONSOLE-LOG", "JS-FETCH-NO-TIMEOUT"}:
                assert h.file.endswith(".js")
