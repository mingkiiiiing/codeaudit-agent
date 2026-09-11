"""合成项目生成器（W5-A2 G-C 压测数据源）：seed 可复现、幂等、ast 全通过。

用法::

    python -m bench.stress.generate --files 2000 --out bench/stress/data/proj2k
    # （``python -m bench.stress.generator`` 等价；bench/stress/generate.py 为 CLI 别名）

    from bench.stress.generator import generate_project
    generate_project(Path("bench/stress/data/proj500"), files=500, seed=42)

结构约定（共 ``files`` 个 .py 文件，精确计数）：
  - ``app/__init__.py`` + ``app/{services,utils,models}/__init__.py``（4 个）+ ``main.py``（1 个）
  - app/services、app/utils、app/models、tests 按权重 0.42/0.27/0.16/0.15 分配剩余名额
  - 文件间真实相互 import（utils→utils、models→utils、services→utils/models、tests→services），
    函数签名统一为 ``(records, factor) -> int``，跨文件调用边数量级 ≥ files；
  - 行数按 files 线性：平均约 90~110 行/文件（500 文件 ≈ 4.5~5.5 万行）；
  - 约 ``defect_ratio``（默认 2%）的 app 文件带一个已知缺陷，模板四种轮转：
    bare_except / mutable_default / sql_concat / eq_none（对应 PY-BARE-EXCEPT、
    PY-MUTABLE-DEFAULT、PY-SQL-INJECTION、PY-EQ-NONE 规则）。

复现与幂等：同样 (files, defect_ratio, seed) 生成字节级一致的文件树；重复调用时若
``<out>.manifest.json`` 的 meta 匹配且文件数一致则直接跳过（幂等）。manifest 落在
项目目录**外**（``<out_dir>.manifest.json``），避免混入被审计文件计数。
"""

from __future__ import annotations

import argparse
import ast
import json
import math
import random
import sys
from pathlib import Path
from typing import Any

# 结构变更时递增：旧数据目录 meta 不匹配会自动重建
GENERATOR_VERSION = "v1"

_MIN_FILES = 9  # 至少容纳 4 个 __init__ + main + 四类角色文件各 1 个（4+1+4）

# 角色文件占比（剩余名额除去 4 个 __init__ 与 main.py 后按此分配）
_ROLE_WEIGHTS: dict[str, float] = {"services": 0.42, "utils": 0.27, "models": 0.16, "tests": 0.15}

# 已知缺陷模板（与 audit/detect/rules/python.py 的规则对应）
DEFECT_KINDS: tuple[str, ...] = ("bare_except", "mutable_default", "sql_concat", "eq_none")

_FN_VERBS = ("load", "blend", "flush", "prune", "merge", "scale", "clip",
             "fold", "digest", "balance", "sweep", "pack")

_ROLE_TAG = {"services": "svc", "utils": "util", "models": "model"}


# ---------------------------------------------------------------- 名额分配


def alloc_counts(files: int) -> dict[str, int]:
    """把 files 个 .py 名额切分为 {services, utils, models, tests, inits, main}。

    固定 4 个 ``__init__.py`` 与 1 个 ``main.py``；其余按 _ROLE_WEIGHTS 最大余数法
    分配（每桶至少 1 个）。结果之和恒等于 files。
    """
    if files < _MIN_FILES:
        raise ValueError(f"files 至少为 {_MIN_FILES}（需容纳 __init__/main 与四类角色文件）")
    remaining = files - 5
    raw = {role: remaining * w for role, w in _ROLE_WEIGHTS.items()}
    counts = {role: max(1, math.floor(v)) for role, v in raw.items()}
    order = sorted(raw, key=lambda r: raw[r] - math.floor(raw[r]), reverse=True)
    # 正差额：按小数部分从大到小逐桶 +1（floor 之后只可能补，不会超）
    diff = remaining - sum(counts.values())
    i = 0
    while diff > 0:
        counts[order[i % len(order)]] += 1
        diff -= 1
        i += 1
    # 负差额：只从 >1 的桶里扣，扣不动即规模不可行（防御性兜底，正常到不了）
    while diff < 0:
        donors = [r for r in order if counts[r] > 1]
        if not donors:
            raise ValueError(f"files={files} 过小，名额分配不可行")
        for role in donors:
            if diff == 0:
                break
            counts[role] -= 1
            diff += 1
    return {"services": counts["services"], "utils": counts["utils"], "models": counts["models"],
            "tests": counts["tests"], "inits": 4, "main": 1}


# ---------------------------------------------------------------- 代码模板


def _fn_lines(name: str, callees: list[str], rng: random.Random, defect: str | None) -> list[str]:
    """生成一个 ``(records, factor) -> int`` 合成函数（12~18 行），返回源码行。"""
    lines = [f"def {name}(records, factor):", '    """合成函数：对 records 做确定性加权累加。"""', "    total = 0"]
    lines += ["    for item in records:", "        weight = factor % (len(str(item)) + 7)"]
    if defect == "eq_none":
        lines += ["        if weight == None:", "            weight = 7"]
    if callees:
        lines.append(f"        total += {callees[0]}([item], weight) % 97")
    else:
        lines.append("        total += weight % 97")
    if len(callees) > 1:
        lines += ["    if factor > 64:", f"        total += {callees[1]}(records[:4], factor // 2)"]
    if defect == "bare_except":
        lines += ["    try:", "        total = total + (factor % 13)", "    except:", "        total = -1"]
    if rng.random() < 0.7:
        lines += ["    bucket = {idx: idx * factor for idx in range(4)}", "    for key in sorted(bucket):",
                  "        total += bucket[key] % 5"]
    if rng.random() < 0.45:
        lines += ["    for chunk in records[:3]:", "        total += (len(str(chunk)) + factor) % 11"]
    lines.append("    return total + len(records)")
    return lines


def _defect_fn_lines(name: str, kind: str) -> list[str]:
    """注入型缺陷函数（mutable_default / sql_concat 两种整函数模板）。"""
    if kind == "mutable_default":
        return [
            f"def {name}_memo(key, cache={{}}):",
            '    """合成缺陷：可变默认参数在多次调用间共享。"""',
            "    if key not in cache:",
            "        cache[key] = len(str(key)) * 3",
            "    return cache[key]",
        ]
    if kind == "sql_concat":
        return [
            f"def {name}_query(owner):",
            '    """合成缺陷：SQL 语句字符串拼接外部输入。"""',
            '    query = "SELECT * FROM audit_events WHERE owner = " + owner',
            '    return {"sql": query, "limit": len(owner) % 10}',
        ]
    raise ValueError(f"非注入型缺陷模板：{kind}")


def _module_source(
    tag: str,
    role: str,
    seed: int,
    fn_names: list[str],
    imports: list[tuple[str, str]],  # (module_dotted, func_name)
    defect: tuple[str, str] | None,  # (kind, anchor_fn)
    rng: random.Random,
) -> str:
    """拼接单个 app 模块源码（docstring + import + 函数 + 注入缺陷）。"""
    lines = [f'"""合成模块 {tag}（role={role}，bench.stress.generator seed={seed} 自动生成，勿手改）。"""', "",
             "from __future__ import annotations", ""]
    seen: set[str] = set()
    for module_dotted, func in imports:
        stmt = f"from {module_dotted} import {func}"
        if stmt not in seen:
            seen.add(stmt)
            lines.append(stmt)
    if seen:
        lines.append("")
    lines += ["", f'MODULE_TAG = "{tag}"', "", ""]
    for i, fn in enumerate(fn_names):
        callees = [f for (_m, f) in imports]
        local_defect = defect[0] if defect and defect[1] == fn else None
        body = _fn_lines(fn, callees if i == 0 else callees[:1], rng, local_defect)
        lines += body
        lines.append("")
        if i < len(fn_names) - 1:
            lines.append("")
    if defect and defect[0] in ("mutable_default", "sql_concat"):
        lines.append("")
        lines += _defect_fn_lines(defect[1], defect[0])
        lines.append("")
    return "\n".join(lines) + "\n"


def _test_source(test_tag: str, target_module: str, target_fn: str, seed: int) -> str:
    """合成单测文件：import 一个 service 函数并写两个冒烟断言（不执行）。"""
    return (
        f'"""合成单测 {test_tag}（bench.stress.generator seed={seed} 自动生成，勿手改）。"""\n'
        "\n"
        f"from {target_module} import {target_fn}\n"
        "\n"
        "\n"
        f"def test_{target_fn}_empty():\n"
        f"    assert {target_fn}([], 3) == 0\n"
        "\n"
        "\n"
        f"def test_{target_fn}_small():\n"
        f"    total = {target_fn}([1, 2], 5)\n"
        "    assert total >= 0\n"
    )


def _main_source(entry_fn: str, seed: int) -> str:
    """入口 main.py：串联一个 service 合成调用。"""
    return (
        f'"""合成入口 main（bench.stress.generator seed={seed} 自动生成，勿手改）。"""\n'
        "\n"
        "from __future__ import annotations\n"
        "\n"
        f"from app.services import {entry_fn}\n"
        "\n"
        "\n"
        "def main(records, factor):\n"
        f"    return {entry_fn}(records, factor)\n"
        "\n"
        "\n"
        'if __name__ == "__main__":\n'
        f"    print(main([1, 2, 3], 4))  # 合成入口：仅演示调用链，不作为质量样板\n"
    )


# ---------------------------------------------------------------- 生成主流程


def _plan_modules(counts: dict[str, int], rng: random.Random) -> list[dict[str, Any]]:
    """生成模块计划（utils → models → services 顺序，保证 import 目标先存在）。

    每个计划项：{role, tag, dotted, rel_path, fn_names, imports, defect}（tests 项
    在 generate_project 中单独拼装）。app 模块按生成顺序返回，供缺陷分配挑选。
    """
    width = 4
    exports: dict[str, list[tuple[str, str, str]]] = {"utils": [], "models": [], "services": []}
    plan: list[dict[str, Any]] = []

    def _app_module(role: str, idx: int) -> None:
        tag = f"{_ROLE_TAG[role]}_{idx:0{width}d}"
        dotted = f"app.{role}.{tag}"
        n_funcs = rng.randint(7, 11)
        verbs = [_FN_VERBS[k % len(_FN_VERBS)] for k in range(n_funcs)]
        fn_names = [f"{tag}_{v}" for v in verbs]
        # import 目标：从先生成的模块中挑 1~3 个函数（utils 只允许 import utils）
        if role == "utils":
            pool = [(m, fn) for (m, fn, _t) in exports["utils"]]
        elif role == "models":
            pool = [(m, fn) for r in ("utils", "models") for (m, fn, _t) in exports[r]]
        else:
            pool = [(m, fn) for r in ("utils", "models", "services") for (m, fn, _t) in exports[r]]
        picks: list[tuple[str, str]] = []
        if pool:
            # 上限 2：_fn_lines 只消费 callees[0]/callees[1]，多取必然产生未用 import
            n_imports = min(len(pool), 2)
            modules = rng.sample(range(len(pool)), n_imports)
            picks = [pool[k] for k in modules]
        plan.append({"role": role, "tag": tag, "dotted": dotted,
                     "rel_path": f"app/{role}/{tag}.py", "fn_names": fn_names,
                     "imports": picks, "defect": None})
        exports[role].append((dotted, fn_names[0], tag))

    for i in range(counts["utils"]):
        _app_module("utils", i)
    for i in range(counts["models"]):
        _app_module("models", i)
    for i in range(counts["services"]):
        _app_module("services", i)
    return plan


def _assign_defects(plan: list[dict[str, Any]], rng: random.Random, n_defects: int) -> dict[str, str]:
    """把 n_defects 个缺陷（模板轮转）分配到 app 模块，返回 {rel_path: kind}。"""
    assigned: dict[str, str] = {}
    if n_defects <= 0:
        return assigned
    candidates = [p for p in plan if p["role"] in ("services", "utils", "models")]
    rng.shuffle(candidates)
    for k in range(min(n_defects, len(candidates))):
        item = candidates[k]
        kind = DEFECT_KINDS[k % len(DEFECT_KINDS)]
        item["defect"] = (kind, item["fn_names"][0])
        assigned[item["rel_path"]] = kind
    return assigned


def generate_project(
    out_dir: Path,
    files: int = 500,
    defect_ratio: float = 0.02,
    seed: int = 42,
    *,
    force: bool = False,
) -> Path:
    """生成分层 Python 合成项目（幂等：meta 一致且文件数吻合则跳过）。

    返回项目目录 ``out_dir``；同时写 ``<out_dir>.manifest.json``（项目目录外），
    含 meta / 文件清单 / 缺陷映射 / 总行数。生成后对全部 .py 做 ast.parse 校验。
    """
    out_dir = Path(out_dir)
    counts = alloc_counts(files)
    n_defects = max(0, round(files * defect_ratio))
    manifest_path = out_dir.parent / f"{out_dir.name}.manifest.json"
    meta = {
        "generator": GENERATOR_VERSION,
        "files": files,
        "defect_ratio": defect_ratio,
        "seed": seed,
        "defects": n_defects,
    }

    if not force and _is_fresh(out_dir, manifest_path, meta, counts):
        return out_dir

    rng = random.Random(seed)
    plan = _plan_modules(counts, rng)
    defect_map = _assign_defects(plan, rng, n_defects)

    if out_dir.exists():
        import shutil

        shutil.rmtree(out_dir)
    (out_dir / "app").mkdir(parents=True)
    (out_dir / "app" / "services").mkdir()
    (out_dir / "app" / "utils").mkdir()
    (out_dir / "app" / "models").mkdir()
    (out_dir / "tests").mkdir()

    init_src = "\n".join([
        f'"""合成包 __init__（bench.stress.generator seed={seed} 自动生成）。"""',
        "",
    ])
    py_sources: dict[str, str] = {
        "app/__init__.py": init_src,
        "app/services/__init__.py": init_src,
        "app/utils/__init__.py": init_src,
        "app/models/__init__.py": init_src,
    }
    per_file_loc: dict[str, int] = {}
    for item in plan:
        file_rng = random.Random(f"{seed}:{item['rel_path']}")  # 每文件独立子流：整体仍 seed 可复现
        src = _module_source(item["tag"], item["role"], seed, item["fn_names"],
                             item["imports"], item["defect"], file_rng)
        py_sources[item["rel_path"]] = src

    services = [p for p in plan if p["role"] == "services"]
    tests_counts = counts["tests"]
    for i in range(tests_counts):
        target = services[i % len(services)]
        rel = f"tests/test_{target['tag']}.py"
        py_sources[rel] = _test_source(f"test_{target['tag']}", target["dotted"], target["fn_names"][0], seed)

    second = services[min(1, len(services) - 1)]["fn_names"][0]
    py_sources["main.py"] = _main_source(second, seed)

    total_loc = 0
    for rel, src in py_sources.items():
        try:
            ast.parse(src)
        except SyntaxError as exc:  # 生成器自身缺陷立即暴露
            raise RuntimeError(f"生成文件 ast.parse 失败：{rel}（{exc}）") from exc
        (out_dir / rel).write_text(src, encoding="utf-8", newline="\n")
        per_file_loc[rel] = src.count("\n")
        total_loc += per_file_loc[rel]

    manifest = {
        "meta": meta,
        "counts": counts,
        "total_loc": total_loc,
        "defects": defect_map,
        "files": sorted(py_sources),
        "per_file_loc": per_file_loc,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return out_dir


def _is_fresh(out_dir: Path, manifest_path: Path, meta: dict[str, Any], counts: dict[str, int]) -> bool:
    """幂等检查：manifest meta 匹配且磁盘 .py 文件数一致。"""
    if not out_dir.is_dir() or not manifest_path.is_file():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if manifest.get("meta") != meta:
        return False
    actual = sum(1 for p in out_dir.rglob("*") if p.is_file() and p.suffix == ".py")
    expected = counts["services"] + counts["utils"] + counts["models"] + counts["tests"] + 5
    return actual == expected


def load_manifest(out_dir: Path) -> dict[str, Any]:
    """读取项目 manifest（不存在抛 FileNotFoundError）。"""
    out_dir = Path(out_dir)
    return json.loads((out_dir.parent / f"{out_dir.name}.manifest.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    """CLI：``python -m bench.stress.generate --files 2000 --out bench/stress/data/proj2k``。"""
    parser = argparse.ArgumentParser(
        prog="python -m bench.stress.generate",
        description="合成压测项目生成器：分层 Python 包 + 真实跨文件 import + 约 2%% 已知缺陷（seed 可复现）。",
    )
    parser.add_argument("--files", type=int, default=500, help="生成的 .py 文件总数（默认 500）")
    parser.add_argument("--out", type=Path, default=None, help="输出目录（缺省 bench/stress/data/proj<files>）")
    parser.add_argument("--defect-ratio", type=float, default=0.02, help="带已知缺陷的文件占比（默认 0.02）")
    parser.add_argument("--seed", type=int, default=42, help="随机种子（默认 42，保证可复现）")
    parser.add_argument("--force", action="store_true", help="忽略幂等检查，强制重建")
    args = parser.parse_args(argv)

    out = args.out or (Path(__file__).resolve().parents[1] / "stress" / "data" / f"proj{args.files}")
    out = generate_project(out, files=args.files, defect_ratio=args.defect_ratio, seed=args.seed,
                           force=args.force)
    manifest = load_manifest(out)
    print(f"[stress.generate] 项目已就绪：{out}")
    print(f"[stress.generate] 文件 {manifest['meta']['files']} 个 ｜ 总行数 {manifest['total_loc']}"
          f" ｜ 缺陷文件 {manifest['meta']['defects']} 个（seed={args.seed}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
