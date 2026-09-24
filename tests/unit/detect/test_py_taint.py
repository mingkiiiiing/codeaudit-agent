"""PY-TAINT-UNSAFE-SINK 行为测试：函数内污点传播（外部输入 source → 危险汇点 sink）。

语料：本文件内联 24 个正例函数 + 12 个干净对照函数（每个正例的 sink 行以
``# SINK`` 注释标记，期望行号由标记行自动推导并逐条精确断言——W18 教训：按行号归因）。
验收口径（W30 卡 A）：正例 ≥20 且实测检出 ≥15（当前实现 24/24）；对照 0 误报；
AST-only 契约：tree=None（无 tree-sitter / 降级路径）时不产任何命中；
同名覆盖（污染后再赋常量 → 不再报）单列用例。
"""

from __future__ import annotations

import re

import pytest

from audit.detect.base import RuleContext
from audit.detect.rules.py_taint import PyTaintUnsafeSinkRule
from audit.indexer.parsers import parse_source

# ---------------------------------------------------------------- 语料（正例）
# 每个正例独立成段：覆盖 source→sink 直达 / 两级 / 三级传播 / f-string / format /
# 拼接 / 容器 / 各 sink 家族 × 各 source 家族组合；sink 行带 `# SINK` 标记。

POSITIVES: dict[str, str] = {
    # -- source→sink 直达（零传播） --
    "direct_sql_concat": '''
def show_user(request, cursor):
    uid = request.args.get("uid")
    cursor.execute("SELECT * FROM users WHERE id=" + uid)  # SINK
''',
    "eval_direct": '''
def run_expr(request):
    eval(request.args.get("expr"))  # SINK
''',
    "os_system_direct": '''
import os

def ping_host(request):
    os.system("ping -c 1 " + request.args.get("host"))  # SINK
''',
    "zero_prop_executemany": '''
def bulk(cursor, rows, request):
    cursor.executemany(request.args.get("sql"), rows)  # SINK
''',
    # -- 两级传播 --
    "two_level_sql": '''
def show_user(request, cursor):
    uid = request.args.get("uid")
    query = "SELECT * FROM users WHERE name='" + uid + "'"
    cursor.execute(query)  # SINK
''',
    "eval_prop_subscript_source": '''
def run_expr(request):
    code = request.form["code"]
    eval(code)  # SINK
''',
    "exec_input": '''
def run_console():
    stmt = input(">>> ")
    exec(stmt)  # SINK
''',
    "os_system_concat": '''
import os

def ping_host(request):
    host = request.values.get("host")
    os.system("ping -c 1 " + host)  # SINK
''',
    "subprocess_run_list": '''
import subprocess

def list_dir(request):
    path = request.args.get("path")
    subprocess.run(["ls", "-l", path])  # SINK
''',
    "sys_argv_sql": '''
import sys

def export(cursor):
    table = sys.argv[1]
    cursor.execute("SELECT * FROM " + table)  # SINK
''',
    "get_json_subscript": '''
def show_user(request, cursor):
    payload = request.get_json()
    uid = payload["id"]
    cursor.execute(f"SELECT * FROM users WHERE id={uid}")  # SINK
''',
    "aug_assign_source": '''
def search(request, cursor):
    query = "SELECT * FROM products WHERE 1=1"
    query += " AND name LIKE '%" + request.args.get("q") + "%'"
    cursor.execute(query)  # SINK
''',
    "annotated_taint": '''
def show_user(request, cursor):
    uid: str = request.args.get("uid")
    cursor.execute(f"SELECT * FROM users WHERE id={uid}")  # SINK
''',
    # -- 三级及以上传播 --
    "three_level_sql": '''
def show_user(request, cursor):
    raw = request.form.get("keyword")
    keyword = raw.strip().lower()
    query = "SELECT * FROM logs WHERE msg LIKE '%" + keyword + "%'"
    cursor.execute(query)  # SINK
''',
    "os_popen_fstring": '''
import os

def trace_route(request):
    host = request.cookies.get("host")
    os.popen(f"traceroute {host}")  # SINK
''',
    "subprocess_popen_shell": '''
import subprocess

def run_tool(request):
    cmd = request.get_data(as_text=True)
    subprocess.Popen(cmd, shell=True)  # SINK
''',
    "subprocess_check_output": '''
import subprocess

def git_log(request):
    rev = request.headers.get("X-Revision")
    spec = rev + " --not --tags"
    subprocess.check_output(["git", "log", spec])  # SINK
''',
    "subprocess_check_call": '''
import subprocess
import sys

def probe():
    target = sys.argv[1]
    subprocess.check_call("ping -c 1 " + target, shell=True)  # SINK
''',
    "sys_argv_method": '''
import os
import sys

def scan():
    target = sys.argv[1].strip()
    os.system("nmap -sV " + target)  # SINK
''',
    # -- 容器传播 --
    "dict_container_sql": '''
def show_user(request, cursor):
    uid = request.args.get("id")
    params = {"id": uid}
    query = f"SELECT * FROM users WHERE id={params['id']}"
    cursor.execute(query)  # SINK
''',
    "list_container_cmd": '''
import os

def greet(request):
    name = request.form.get("name")
    pieces = [name]
    os.system("echo " + pieces[0])  # SINK
''',
    "tuple_unpack_chain": '''
import os

def handle(request):
    raw = request.form.get("cmd")
    first, flag = raw, "--force"
    os.system("run-tool " + first)  # SINK
''',
    # -- f-string / format 构造 --
    "fstring_sql": '''
def show_user(request, cursor):
    uid = request.args.get("id")
    cursor.execute(f"SELECT * FROM users WHERE id={uid}")  # SINK
''',
    "format_sql_executemany": '''
def bulk_update(prompt, cursor, rows):
    uid = input(prompt)
    query = "UPDATE users SET last_seen=NOW() WHERE id={}".format(uid)
    cursor.executemany(query, rows)  # SINK
''',
}

# ---------------------------------------------------------------- 语料（干净对照）
# 参数化 SQL / 常量 execute / 普通函数间传值（参数不作 source）/ 污染未触 sink /
# 同名覆盖洗白 / 非 sink 家族方法 / 常量命令 / request 别名（已知边界不识别）等。

CLEANS: dict[str, str] = {
    "parameterized_sql": '''
def show_user(request, cursor):
    query = "SELECT * FROM users WHERE id=?"
    uid = request.args.get("uid")
    cursor.execute(query, (uid,))  # 参数元组是参数化通道，不应报
''',
    "const_execute": '''
def dump(cursor):
    cursor.execute("SELECT * FROM users")
''',
    "param_not_source": '''
def render(cursor, uid):
    query = "SELECT * FROM users WHERE id=" + uid
    cursor.execute(query)
''',
    "tainted_no_sink": '''
def log_visit(request, logger):
    uid = request.args.get("uid")
    logger.info("visit by %s", uid)
''',
    "reassigned_constant": '''
def show_user(request, cursor):
    uid = request.args.get("uid")
    uid = "42"
    cursor.execute("SELECT * FROM users WHERE id=" + uid)
''',
    "bare_attr_ref_no_call": '''
def lookup(request, cursor):
    uid = request.args.get("uid")
    cursor.execute  # 裸属性引用（未调用）
    return cursor.fetchone()
''',
    "eval_const": '''
def default_expr():
    return eval("1 + 1")
''',
    "write_not_sink": '''
def save_note(request, out):
    note = request.form.get("note")
    out.write(note)
''',
    "subprocess_const": '''
import subprocess

def list_dir():
    subprocess.run(["ls", "-l"], check=True)
''',
    "argv_count_only": '''
import sys

def report(logger):
    logger.info("argc=%d", len(sys.argv))
''',
    "request_alias_boundary": '''
def show_user(request, cursor):
    data = request
    uid = data.args.get("id")  # 已知边界：request 别名不识别（source 丢失，同时不误报）
    cursor.execute("SELECT * FROM users WHERE id=" + uid)
''',
    "cross_function_return": '''
def fetch_name(request):
    return request.args.get("name")

def greet(cursor, name):
    cursor.execute("SELECT * FROM users WHERE name='" + name + "'")
''',
}


# ---------------------------------------------------------------- 公共工具


def _ctx(source: str, rel_path: str = "taint_case.py", *, with_tree: bool) -> RuleContext:
    tree = None
    if with_tree:
        tree, _ok = parse_source("python", source.encode("utf-8", errors="replace"))
        assert tree is not None, "tree-sitter python 解析器不可用，测试环境异常"
    return RuleContext(
        rel_path=rel_path,
        language="python",
        source=source,
        lines=source.splitlines(),
        tree=tree,
    )


def _sink_line(source: str) -> int:
    """从语料文本推导期望 sink 行号（`# SINK` 标记所在行，1-based）。"""
    for lineno, line in enumerate(source.splitlines(), 1):
        if "# SINK" in line:
            return lineno
    raise AssertionError("语料缺少 # SINK 标记（测试自身缺陷）")


rule = PyTaintUnsafeSinkRule()


# ---------------------------------------------------------------- 正例逐条断言


@pytest.mark.parametrize("case_name", sorted(POSITIVES))
def test_positive_hits_expected_sink_line(case_name: str):
    """每个正例恰好一条命中，且行号精确落在 # SINK 标记行。"""
    src = POSITIVES[case_name]
    hits = rule.check(_ctx(src, with_tree=True))
    expected = _sink_line(src)
    assert [h.line_start for h in hits] == [expected], (
        f"用例 {case_name} 命中行不符：{[h.line_start for h in hits]} != [{expected}]"
    )
    hit = hits[0]
    assert hit.rule_id == "PY-TAINT-UNSAFE-SINK"
    assert hit.severity.name == "HIGH"
    assert hit.category.name == "SECURITY"


@pytest.mark.parametrize("case_name", sorted(POSITIVES))
def test_positive_chain_meta_shape(case_name: str):
    """meta.chain 首项为 source 事件、末项为 sink 事件，file:L行 形态可读。"""
    src = POSITIVES[case_name]
    hit = rule.check(_ctx(src, with_tree=True))[0]
    chain = hit.meta["chain"]
    assert isinstance(chain, list) and len(chain) >= 2
    assert re.fullmatch(r"taint_case\.py:L\d+ source: \S+", chain[0]), chain[0]
    assert re.fullmatch(r"taint_case\.py:L\d+ sink: \S+", chain[-1]), chain[-1]


def test_positive_detection_meets_acceptance_bar():
    """验收硬线：24 正例至少检出 15（当前实现应全检出）。"""
    detected = [name for name, src in POSITIVES.items() if rule.check(_ctx(src, with_tree=True))]
    assert len(detected) >= 15, f"检出不足：{len(detected)}/15，漏报 {sorted(set(POSITIVES) - set(detected))}"


# ---------------------------------------------------------------- 干净对照


@pytest.mark.parametrize("case_name", sorted(CLEANS))
def test_clean_zero_false_positive(case_name: str):
    """干净对照零误报（含参数化 SQL、同名覆盖洗白、普通函数间传值）。"""
    src = CLEANS[case_name]
    assert "# SINK" not in src, "对照语料不应带 SINK 标记"
    assert rule.check(_ctx(src, with_tree=True)) == []


# ---------------------------------------------------------------- 专项行为


def test_reassign_constant_clears_taint_contrast():
    """同名覆盖对照：同一函数去掉洗白行即命中，保留洗白行不命中。"""
    tainted = '''
def show_user(request, cursor):
    uid = request.args.get("uid")
    cursor.execute("SELECT * FROM users WHERE id=" + uid)  # SINK
'''
    washed = '''
def show_user(request, cursor):
    uid = request.args.get("uid")
    uid = "42"
    cursor.execute("SELECT * FROM users WHERE id=" + uid)
'''
    assert [h.line_start for h in rule.check(_ctx(tainted, with_tree=True))] == [4]
    assert rule.check(_ctx(washed, with_tree=True)) == []


def test_same_line_multi_sink_dedup_to_one_hit():
    """同一 sink 行多链（同行两个汇点 / 多污点入同一汇点）去重为一条命中。"""
    two_sinks_same_line = '''
def run_all(request):
    a = request.args.get("a")
    exec(a); eval(a)  # SINK（两个汇点同行 → 一条命中）
'''
    hits = rule.check(_ctx(two_sinks_same_line, with_tree=True))
    assert [h.line_start for h in hits] == [4]

    multi_chain_one_sink = '''
def show_user(request, cursor):
    a = request.args.get("a")
    b = request.form.get("b")
    cursor.execute("SELECT * FROM t WHERE x='" + a + b + "'")  # SINK（多污点同汇点 → 一条命中）
'''
    hits = rule.check(_ctx(multi_chain_one_sink, with_tree=True))
    assert [h.line_start for h in hits] == [5]


def test_chain_levels_reported_in_meta():
    """三级传播链：chain = source → prop → prop → sink，levels=2。"""
    src = POSITIVES["three_level_sql"]
    hit = rule.check(_ctx(src, with_tree=True))[0]
    chain = hit.meta["chain"]
    assert len(chain) == 4
    assert "source: request.form.get" in chain[0]
    assert "keyword = " in chain[1]
    assert "query = " in chain[2]
    assert "sink: cursor.execute" in chain[3]
    assert hit.meta["levels"] == 2


@pytest.mark.parametrize("case_name", sorted(POSITIVES) + sorted(CLEANS))
def test_ast_only_contract_no_hit_without_tree(case_name: str):
    """tree=None（行级兜底路径）时 AST-only 规则不产任何命中、不报错。"""
    src = POSITIVES.get(case_name) or CLEANS[case_name]
    assert rule.check(_ctx(src, with_tree=False)) == []


# ---------------------------------------------------------------- 注册表接线（P0-13）


def test_taint_rule_reachable_via_registry():
    """接线契约：规则必须经 DEFAULT_REGISTRY 主链路可达（engine 全链路消费口径）。

    W30 半收口教训（F10-R1）：规则类直测全绿但 registry 漏 register_all，
    CLI/server/CI/前端四入口零命中——本断言钉住「import 了必须注册了」。
    """
    from audit.detect.registry import DEFAULT_REGISTRY

    assert "PY-TAINT-UNSAFE-SINK" in {r.id for r in DEFAULT_REGISTRY.rules_for("python")}
    all_ids = [r.id for r in DEFAULT_REGISTRY.all_rules]
    assert all_ids.count("PY-TAINT-UNSAFE-SINK") == 1
