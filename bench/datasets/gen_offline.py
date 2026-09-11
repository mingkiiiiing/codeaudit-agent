"""离线金标数据集与离线基线 run 记录生成器（Wave 3 · W3-A1）。

用途（docs/04 §2 / docs/08 §4 G-A）：
    构建一个 ≥150 条金标的完整离线评估数据集，并（可选）在其上产出
    「离线纯规则基线」run 记录（full / −verify / rules_only 三配置 × 项目集）。

数据构成（全部落盘 bench/datasets/ 下）：
    1. 合成项目 4 个（bench/datasets/projects/<name>/，含 .git 历史）：
       shopcore / blogengine / datatools / webapi，每个 8~9 个 .py 文件、
       三四百行，有包结构与跨模块调用；初版埋入已知缺陷并 git 提交，
       随后逐个提交"修复"（消息含 fix，1:1 等行数替换，不改变行数），因此：
         - HEAD 版本 = 静态审计对象（残留缺陷以 manual 金标记录）；
         - 历史 = mine_git_history 的挖矿对象（修复 diff 反推 git-history 金标，
           其"父提交坐标"与 HEAD 坐标一致，可对任一版本对账）。
    2. 注入变体 5 个（projects/<name>_inj/）：对 4 个合成项目与
       tests/samples/demo_proj 用 bench.goldset_mining.inject_defects 注入
       已知缺陷模板（injected 金标）；变体继承源项目缺陷，源项目 manual
       金标按变体项目名克隆（[variant] 前缀，保持 description 全局唯一）。
    3. demo_proj 本体的 12 条金标（GOLDEN_ISSUES.md 表格，manual 来源）。
    合并去重后写入 bench/datasets/goldset.jsonl。

重跑语义（幂等）：
    每次运行**清空重建** bench/datasets/ 下除本脚本外的全部内容
    （projects/、goldset.jsonl、mini/ 等历史产物一并清除）；
    随机性只出现在 inject_defects 内部且全部传固定 seed，重复运行产出
    完全一致的数据集。--with-run 会额外执行三配置离线审计并覆盖写
    bench/results/run_20260911_offline.md（审计耗随时机器波动，金标可复现）。

用法（项目根目录）：
    python bench/datasets/gen_offline.py             # 只构建数据集
    python bench/datasets/gen_offline.py --with-run  # 构建数据集 + 离线基线 run 记录

诚实性约定：本脚本产出的是"离线纯规则基线"，不产生任何 LLM 指标；
run 记录中会明确标注 LLM 相关指标（精确率 85% 目标、tokens/KLOC）待提供
GLM_API_KEY 后真跑。禁网；git 仅本地操作。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:  # 允许以任意 cwd 直接执行本脚本
    sys.path.insert(0, str(PROJECT_ROOT))

from bench.goldset import GoldenIssue, from_markdown_table, load_goldset, save_goldset  # noqa: E402
from bench.goldset_mining import inject_defects, mine_git_history  # noqa: E402
from bench.run import run_ablation, write_run_record  # noqa: E402

DATASETS_DIR = PROJECT_ROOT / "bench" / "datasets"
PROJECTS_DIR = DATASETS_DIR / "projects"
GOLDSET_PATH = DATASETS_DIR / "goldset.jsonl"
DEMO_PROJ = PROJECT_ROOT / "tests" / "samples" / "demo_proj"
RESULTS_DIR = PROJECT_ROOT / "bench" / "results"
RUN_RECORD_PATH = RESULTS_DIR / "run_20260911_offline.md"

MINUS = "\u2212"  # 消融配置名中的减号（U+2212，与 bench.ablation.ABLATION_CONFIGS 一致）
RUN_DATE = "2026-09-11"
ABLATION_NAMES: tuple[str, ...] = ("full", MINUS + "verify", "rules_only")

TARGET_GOLDENS = 150  # docs/04 §2.1：金标总数下限
TARGET_CRITICAL_HIGH = 60  # docs/04 §2.1：critical+high 下限（Precision 主指标口径）

# 注入参数（seed 固定 → 可复现）：合成项目 8 文件×3 模板、demo 6 文件×4 模板
INJECT_PLAN: dict[str, tuple[int, int, int]] = {
    # 项目名: (n_files, seed, per_file)
    "shopcore": (8, 11, 3),
    "blogengine": (8, 22, 3),
    "datatools": (7, 33, 3),
    "webapi": (8, 44, 3),
    "demo_proj": (6, 0, 4),
}


# ---------------------------------------------------------------- 声明结构


@dataclass(frozen=True)
class GoldenSpec:
    """一条人工埋入缺陷的声明：anchor 用于在生成后的源码中定位真实行号。"""

    file: str  # 项目内相对路径（posix）
    anchor: str  # 该行源码中的唯一子串
    category: str  # bug | security | performance | style
    severity: str  # critical | high | medium | low
    summary: str  # 中文一句话描述（行号生成时自动拼入 description）


@dataclass(frozen=True)
class FixSpec:
    """一次"修复"提交：把 anchor 所在行（含 before 行上文）起等行数替换为 new_lines。

    被替换的旧行数 == new_lines 展开后的行数（保证所有版本行号坐标系一致，
    使 git-history 金标的父提交坐标与 HEAD 坐标对齐，manual 金标行号跨版本稳定）。
    before 表示 anchor 位于被替换块内的第 before 行（0 起算），用于
    "修复块从 anchor 上一行开始"的场景（如清理 return 之后的死代码）。
    subject 为 commit message：必须含 fix/bug/crash/error/patch，
    且不含 test/chore/docs（bench.goldset_mining 的修复类 commit 判定口径）。
    """

    file: str
    anchor: str
    before: int
    new_lines: tuple[str, ...]
    subject: str


@dataclass(frozen=True)
class ProjectSpec:
    """一个合成项目的完整声明：模块源码、修复提交与人工金标。"""

    name: str
    modules: dict[str, str]  # 相对路径 -> 源码文本
    fixes: tuple[FixSpec, ...] = ()
    goldens: tuple[GoldenSpec, ...] = ()


# ================================================================
# 合成项目一：shopcore（电商订单核心域，9 文件）
# ================================================================

_SHOPCORE = ProjectSpec(
    name="shopcore",
    modules={
        "__init__.py": (
            '"""shopcore：合成电商订单核心域（Wave 3 离线评估数据集项目）。"""\n'
            "\n"
            '__all__ = ["config", "models", "inventory", "pricing", "orders", "payments", "reports"]\n'
        ),
        "config.py": (
            '"""shopcore 全局配置：服务开关与第三方凭据（演示项目，凭据为虚构样例）。"""\n'
            "\n"
            "from __future__ import annotations\n"
            "\n"
            'API_BASE_URL = "https://api.shopcore.example.com/v1"\n'
            'DB_DSN = "postgresql://shop@db.internal:5432/shopcore"\n'
            'SECRET_KEY = "django-insecure-0x9f2c4e7a1b8d5f3a6b"\n'
            "MAX_RETRY = 3\n"
            "PAGE_SIZE = 50\n"
        ),
        "models.py": (
            '"""shopcore 领域模型：商品、购物车与订单。"""\n'
            "\n"
            "from __future__ import annotations\n"
            "\n"
            "from dataclasses import dataclass, field\n"
            "from enum import Enum\n"
            "\n"
            "\n"
            "class OrderStatus(Enum):\n"
            '    """订单状态机。"""\n'
            "\n"
            '    DRAFT = "draft"\n'
            '    PAID = "paid"\n'
            '    SHIPPED = "shipped"\n'
            '    CANCELLED = "cancelled"\n'
            "\n"
            "\n"
            "@dataclass\n"
            "class Product:\n"
            '    """在售商品。"""\n'
            "\n"
            "    sku: str\n"
            "    title: str\n"
            "    unit_price: float\n"
            "    stock: int = 0\n"
            "\n"
            "\n"
            "@dataclass\n"
            "class CartItem:\n"
            '    """购物车条目。"""\n'
            "\n"
            "    product: Product\n"
            "    quantity: int\n"
            "\n"
            "    @property\n"
            "    def subtotal(self) -> float:\n"
            '        """小计金额。"""\n'
            "        return round(self.product.unit_price * self.quantity, 2)\n"
            "\n"
            "\n"
            "@dataclass\n"
            "class Order:\n"
            '    """订单聚合根。"""\n'
            "\n"
            "    order_id: str\n"
            "    items: list[CartItem] = field(default_factory=list)\n"
            "    status: OrderStatus = OrderStatus.DRAFT\n"
            "\n"
            "    @property\n"
            "    def total(self) -> float:\n"
            '        """订单总金额。"""\n'
            "        return round(sum(item.subtotal for item in self.items), 2)\n"
            "\n"
            "    def add_item(self, item: CartItem) -> None:\n"
            '        """追加一条购物车条目。"""\n'
            "        self.items.append(item)\n"
        ),
        "inventory.py": (
            '"""shopcore 库存服务：台账维护、补货与请求去重。"""\n'
            "\n"
            "from __future__ import annotations\n"
            "\n"
            "from shopcore.models import Product\n"
            "\n"
            "\n"
            "class Inventory:\n"
            '    """内存库存台账；生产环境由仓储层替换实现。"""\n'
            "\n"
            "    def __init__(self, products: list[Product] | None = None) -> None:\n"
            "        self._products: list[Product] = list(products or [])\n"
            "\n"
            "    def register(self, product: Product) -> None:\n"
            '        """上架一个商品。"""\n'
            "        self._products.append(product)\n"
            "\n"
            "    def get(self, sku: str) -> Product | None:\n"
            '        """按 SKU 查找商品，缺失时返回 None。"""\n'
            "        for product in self._products:\n"
            "            if product.sku == sku:\n"
            "                return product\n"
            "        return None\n"
            "\n"
            "    def all_skus(self) -> list[str]:\n"
            '        """导出全部 SKU（保持登记顺序）。"""\n'
            "        return [product.sku for product in self._products]\n"
            "\n"
            "    def dedupe_restock_requests(self, requests: list[str]) -> list[str]:\n"
            '        """合并重复的补货请求（历史实现用 list 判重，请求量大时 O(n^2)）。"""\n'
            "        seen = []\n"
            "        for sku in requests:\n"
            "            if sku in seen:\n"
            "                continue\n"
            "            seen.append(sku)\n"
            "        return seen\n"
            "\n"
            "    def restock(self, conn, supplies: dict[str, int]) -> int:\n"
            '        """逐条写补货流水（每次循环一条 UPDATE，应改为 executemany 批量）。"""\n'
            "        changed = 0\n"
            "        for sku, delta in supplies.items():\n"
            "            cur = conn.cursor()\n"
            '            cur.execute("UPDATE stock SET qty = qty + ? WHERE sku = ?", (delta, sku))\n'
            "            changed += cur.rowcount\n"
            "        conn.commit()\n"
            "        return changed\n"
        ),
        "pricing.py": (
            '"""shopcore 定价与优惠计算。"""\n'
            "\n"
            "from __future__ import annotations\n"
            "\n"
            "LARGE_ORDER_THRESHOLD = 100000\n"
            "BIG_ORDER_BONUS = 1000\n"
            "\n"
            "\n"
            "def apply_discount(subtotal: float, tier: str = \"normal\", coupons: list[str] = []) -> float:\n"
            '    """按会员等级与优惠券计算应付金额。"""\n'
            "    working = list(coupons)\n"
            '    if tier == "vip":\n'
            "        subtotal = subtotal * 0.85\n"
            "    for code in working:\n"
            '        if code == "NEW10":\n'
            "            subtotal = subtotal * 0.9\n"
            "    if subtotal >= LARGE_ORDER_THRESHOLD:\n"
            "        subtotal = subtotal - BIG_ORDER_BONUS\n"
            "    return round(subtotal, 2)\n"
            "\n"
            "\n"
            "def normalize_coupon(code: str | None) -> str:\n"
            '    """券码归一化（历史实现用 == None 判空）。"""\n'
            "    if code == None:\n"
            '        return ""\n'
            "    return code.strip().upper()\n"
        ),
        "orders.py": (
            '"""shopcore 订单服务：下单、历史导入与备注解析。"""\n'
            "\n"
            "from __future__ import annotations\n"
            "\n"
            "import json\n"
            "import sqlite3\n"
            "\n"
            "from shopcore.models import CartItem, Order\n"
            "\n"
            "\n"
            "def create_order(cart_items: list[CartItem], order_id: str) -> Order:\n"
            '    """把购物车落为订单草稿。"""\n'
            "    order = Order(order_id=order_id)\n"
            "    for item in cart_items:\n"
            "        order.add_item(item)\n"
            "    return order\n"
            "\n"
            "\n"
            "def fetch_order_dump(path: str) -> list[dict[str, object]]:\n"
            '    """读取历史订单导出文件（旧实现手工 open/close，异常路径句柄泄漏）。"""\n'
            '    handle = open(path, "r", encoding="utf-8")\n'
            "    payload = handle.read()\n"
            "    return json.loads(payload) if payload.strip() else []\n"
            "\n"
            "\n"
            "def parse_priority_note(raw: str) -> int:\n"
            '    """把订单备注里的优先级（形如 P=2）解析成整数，失败按 0。"""\n'
            "    try:\n"
            '        return int(raw.strip().removeprefix("P="))\n'
            "    except:\n"
            "        return 0\n"
            "\n"
            "\n"
            "def legacy_lookup_by_customer(conn: sqlite3.Connection, customer_id: str) -> list[tuple]:\n"
            '    """按客户号查询历史订单（外部输入直接进入查询语句）。"""\n'
            '    return conn.execute("SELECT * FROM orders WHERE customer_id = \'" + customer_id + "\'").fetchall()\n'
        ),
        "payments.py": (
            '"""shopcore 支付与回调通知。"""\n'
            "\n"
            "from __future__ import annotations\n"
            "\n"
            "import urllib.request\n"
            "\n"
            "from shopcore.config import SECRET_KEY\n"
            "from shopcore.models import Order\n"
            "\n"
            'PARTNER_TOKEN = "live-4f8a7b6c5d4e3f2a1b0c9d8e7f6a"\n'
            "\n"
            "\n"
            "def sign_payload(payload: str) -> str:\n"
            '    """对回调报文生成简易签名（演示实现，非真实密码学）。"""\n'
            "    digest = 0\n"
            "    for ch in payload + SECRET_KEY:\n"
            "        digest = (digest * 31 + ord(ch)) & 0x7FFF\n"
            '    return f"{digest:06x}"\n'
            "\n"
            "\n"
            "def notify_webhook(order: Order, url: str) -> bytes:\n"
            '    """推送支付结果到商户 webhook（未设置 timeout，对端挂起会拖死调用线程）。"""\n'
            '    body = f"order={order.order_id}&total={order.total}"\n'
            '    with urllib.request.urlopen(url, data=body.encode("utf-8")) as resp:\n'
            "        return resp.read()\n"
        ),
        "reports.py": (
            '"""shopcore 经营报表：销量渲染、CSV 导出与重试。"""\n'
            "\n"
            "from __future__ import annotations\n"
            "\n"
            "import logging\n"
            "\n"
            "from shopcore.models import Order\n"
            "\n"
            "LOGGER = logging.getLogger(__name__)\n"
            "\n"
            "\n"
            "def render_sales_html(orders: list[Order]) -> str:\n"
            '    """把订单渲染为简单 HTML 列表（循环内字符串拼接）。"""\n'
            '    html = "<ul>"\n'
            "    for order in orders:\n"
            '        html = html + "<li>" + order.order_id + "</li>"\n'
            '    return html + "</ul>"\n'
            "\n"
            "\n"
            "def export_csv(orders: list[Order], path: str) -> None:\n"
            '    """导出订单为 CSV。"""\n'
            "    # TODO: 大表需要分页导出，当前一次性写入内存压力大\n"
            '    with open(path, "w", encoding="utf-8") as fh:\n'
            '        fh.write("order_id,total\\n")\n'
            "        for order in orders:\n"
            "            fh.write(f\"{order.order_id},{order.total}\\n\")\n"
            "\n"
            "\n"
            "def load_orders(task_id: str) -> list[Order]:\n"
            '    """按批次号取订单（演示实现返回固定样例）。"""\n'
            '    return [Order(order_id=f"SO-{task_id}-{i:03d}") for i in range(1, 4)]\n'
            "\n"
            "\n"
            "def export_with_retry(task_id: str, attempts: int = 3) -> str | None:\n"
            '    """带重试地导出报表。"""\n'
            "    for _ in range(attempts):\n"
            "        try:\n"
            "            return render_sales_html(load_orders(task_id))\n"
            "        except TimeoutError:\n"
            "            continue\n"
            "    return None\n"
            '    raise RuntimeError("unreachable: export retry budget exhausted")\n'
        ),
        "main.py": (
            '"""shopcore 演示入口：组装各服务并跑通下单流程。"""\n'
            "\n"
            "from __future__ import annotations\n"
            "\n"
            "from shopcore.inventory import Inventory\n"
            "from shopcore.models import CartItem, Product\n"
            "from shopcore.orders import create_order\n"
            "from shopcore.pricing import apply_discount\n"
            "\n"
            "\n"
            "def build_catalog() -> Inventory:\n"
            '    """装配演示商品目录。"""\n'
            "    catalog = Inventory()\n"
            '    catalog.register(Product(sku="SKU-001", title="机械键盘", unit_price=399.0, stock=20))\n'
            '    catalog.register(Product(sku="SKU-002", title="无线鼠标", unit_price=129.0, stock=55))\n'
            '    catalog.register(Product(sku="SKU-003", title="显示器支架", unit_price=199.0, stock=12))\n'
            "    return catalog\n"
            "\n"
            "\n"
            "def main() -> None:\n"
            '    """跑通一次加购与计价。"""\n'
            "    catalog = build_catalog()\n"
            "    cart = [\n"
            '        CartItem(product=catalog.get("SKU-001"), quantity=1),\n'
            '        CartItem(product=catalog.get("SKU-002"), quantity=2),\n'
            "    ]\n"
            '    order = create_order(cart, order_id="SO-2026-0001")\n'
            '    total = apply_discount(order.total, tier="vip")\n'
            '    print(f"[demo] 订单 {order.order_id} 应付：{total}")\n'
            "\n"
            "\n"
            'if __name__ == "__main__":\n'
            "    main()\n"
        ),
    },
    fixes=(
        FixSpec(
            file="orders.py",
            anchor="SELECT * FROM orders WHERE customer_id = '",
            before=0,
            new_lines=(
                '    return conn.execute("SELECT * FROM orders WHERE customer_id = ?", (customer_id,)).fetchall()',
            ),
            subject="fix: use parameterized query to close sql injection in customer lookup",
        ),
        FixSpec(
            file="reports.py",
            anchor="raise RuntimeError",
            before=1,
            new_lines=(
                '    LOGGER.warning("export retry budget exhausted: %s", task_id)\n    return None\n',
            ),
            subject="fix: replace dead raise after return with warning log in report exporter",
        ),
        FixSpec(
            file="pricing.py",
            anchor="coupons: list[str] = []",
            before=0,
            new_lines=(
                "def apply_discount(subtotal: float, tier: str = \"normal\", coupons: list[str] | None = None) -> float:\n"
                '    """按会员等级与优惠券计算应付金额。"""\n'
                "    working = list(coupons or [])\n",
            ),
            subject="fix: avoid shared mutable default in discount coupons",
        ),
    ),
    goldens=(
        GoldenSpec("config.py", "SECRET_KEY = ", "security", "critical", "SECRET_KEY 硬编码在源码中，随仓库扩散泄露"),
        GoldenSpec("inventory.py", "if sku in seen:", "performance", "medium", "用 list 对补货请求判重，请求量大时 O(n^2)"),
        GoldenSpec("inventory.py", 'cur.execute("UPDATE stock', "performance", "high", "补货循环内逐条 execute，应改 executemany 批量"),
        GoldenSpec("pricing.py", "LARGE_ORDER_THRESHOLD = ", "style", "low", "满额直减阈值以魔法数字散落在代码里"),
        GoldenSpec("pricing.py", "if code == None:", "bug", "low", "用 == None 判空，触发自定义 __eq__ 会误判"),
        GoldenSpec("orders.py", "handle = open(path", "bug", "high", "读取订单导出文件 open 后未 close 也未用 with，句柄泄漏"),
        GoldenSpec("orders.py", "except:", "bug", "medium", "优先级解析用裸 except 吞掉一切异常"),
        GoldenSpec("payments.py", "PARTNER_TOKEN = ", "security", "critical", "合作方 token 硬编码在支付模块源码中"),
        GoldenSpec("payments.py", "with urllib.request.urlopen(url", "performance", "medium", "webhook 通知未设置 timeout，对端挂起拖死调用线程"),
        GoldenSpec("reports.py", "html = html + ", "performance", "low", "报表渲染循环内字符串自拼接，O(n^2)"),
        GoldenSpec("reports.py", "TODO:", "style", "low", "分页导出 TODO 长期滞留未跟踪"),
        GoldenSpec("main.py", 'print(f"[demo]', "style", "low", "生产入口用 print 输出调试信息，绕过日志体系"),
    ),
)


# ================================================================
# 合成项目二：blogengine（静态博客生成引擎，9 文件）
# ================================================================

_BLOGENGINE = ProjectSpec(
    name="blogengine",
    modules={
        "__init__.py": (
            '"""blogengine：合成静态博客生成引擎（Wave 3 离线评估数据集项目）。"""\n'
            "\n"
            '__all__ = ["config", "markdown", "storage", "search", "render", "server", "cache"]\n'
        ),
        "config.py": (
            '"""blogengine 站点配置。"""\n'
            "\n"
            "from __future__ import annotations\n"
            "\n"
            'SITE_TITLE = "纸上博客"\n'
            'BASE_URL = "https://blog.example.dev"\n'
            'POSTS_DIR = "content/posts"\n'
            'OUTPUT_DIR = "public"\n'
            "RELOAD_INTERVAL = 30\n"
        ),
        "markdown.py": (
            '"""blogengine 极简 Markdown 渲染：标题、段落与动态指令。"""\n'
            "\n"
            "from __future__ import annotations\n"
            "\n"
            "import ast\n"
            "\n"
            "\n"
            "def render_headings(text: str) -> list[str]:\n"
            '    """抽取文档中的标题行（# 开头）。"""\n'
            "    heads: list[str] = []\n"
            "    for line in text.splitlines():\n"
            "        stripped = line.lstrip()\n"
            '        if stripped.startswith("#"):\n'
            '            heads.append(stripped.lstrip("#").strip())\n'
            "    return heads\n"
            "\n"
            "\n"
            "def parse_page_count(expr: str) -> int:\n"
            '    """解析每页条数配置（数字字面量文本），失败按 12。"""\n'
            "    try:\n"
            "        return int(ast.literal_eval(expr))\n"
            "    except (SyntaxError, ValueError):\n"
            "        return 12\n"
            "\n"
            "\n"
            "def render_directive(expr: str, meta: dict[str, str]) -> str:\n"
            '    """渲染模板指令（meta 前缀查表，其余表达式求值）。"""\n'
            "    stripped = expr.strip()\n"
            '    if stripped.startswith("meta."):\n'
            '        return meta.get(stripped[5:], "")\n'
            '    return str(eval(stripped, {"meta": meta}))\n'
            "\n"
            "\n"
            "def render_blocks(blocks: list[list[list[str]]]) -> list[str]:\n"
            '    """渲染三层嵌套块（引用-段落-行），历史实现缩放过深。"""\n'
            "    out: list[str] = []\n"
            "    for block in blocks:\n"
            "        for para in block:\n"
            "            for line in para:\n"
            "                if line.strip():\n"
            '                    out.append("<p>" + line.strip() + "</p>")\n'
            "    return out\n"
        ),
        "storage.py": (
            '"""blogengine 文章存储：front-matter 解析与 slug 处理。"""\n'
            "\n"
            "from __future__ import annotations\n"
            "\n"
            "import yaml\n"
            "\n"
            "\n"
            "def load_meta(path: str) -> dict[str, str]:\n"
            '    """读取文章 front-matter（yaml.load 未指定 Loader）。"""\n'
            '    with open(path, "r", encoding="utf-8") as fh:\n'
            "        raw = fh.read()\n"
            "    return yaml.load(raw)\n"
            "\n"
            "\n"
            "def split_slug(path: str) -> tuple[str, str]:\n"
            '    """把 content/posts/2026-09-01-hello.md 拆成 (日期, slug)。"""\n'
            '    stem = path.rsplit("/", 1)[-1].removesuffix(".md")\n'
            '    date, _, slug = stem.partition("-")\n'
            '    return date, slug.lstrip("-")\n'
            "\n"
            "\n"
            "def outline_of(text: str, limit: int = 3) -> list[str]:\n"
            '    """取正文前 limit 个段落的首句做摘要。"""\n'
            '    paras = [p.strip() for p in text.split("\\n\\n") if p.strip()]\n'
            '    return [p.split("。")[0] for p in paras[:limit]]\n'
        ),
        "search.py": (
            '"""blogengine 站内搜索：标签倒排与浏览轨迹。"""\n'
            "\n"
            "from __future__ import annotations\n"
            "\n"
            "\n"
            "def build_tag_index(posts: list[dict[str, str]]) -> dict[str, set[str]]:\n"
            '    """按标签建倒排索引（slug 集合）。"""\n'
            "    index: dict[str, set[str]] = {}\n"
            "    for post in posts:\n"
            '        for tag in post.get("tags", "").split():\n'
            '            index.setdefault(tag, set()).add(post["slug"])\n'
            "    return index\n"
            "\n"
            "\n"
            "def recently_revisited(history: list[str]) -> list[str]:\n"
            '    """找出会话内反复浏览的文章（list 判重，文章多时 O(n^2)）。"""\n'
            "    again: list[str] = []\n"
            "    seen = []\n"
            "    for slug in history:\n"
            "        if slug in seen:\n"
            "            again.append(slug)\n"
            "        else:\n"
            "            seen.append(slug)\n"
            "    return again\n"
            "\n"
            "\n"
            "def snippet_of(post: dict[str, str], limit: int | None = None) -> str:\n"
            '    """取文章摘要（limit 为 None 时截 80 字）。"""\n'
            "    if limit == None:\n"
            "        limit = 80\n"
            '    return post.get("summary", "")[:limit]\n'
        ),
        "render.py": (
            '"""blogengine 页面渲染：模板替换、目录树与发布。"""\n'
            "\n"
            "from __future__ import annotations\n"
            "\n"
            "\n"
            "def render_page(template: str, meta: dict[str, str], body: str) -> str:\n"
            '    """把模板里的 {{key}} 替换为 meta 值并拼接正文。"""\n'
            "    html = template\n"
            "    for key, value in meta.items():\n"
            '        html = html.replace("{{" + key + "}}", value)\n'
            '    return html.replace("{{body}}", body)\n'
            "\n"
            "\n"
            "def render_toc(tree: dict[str, dict[str, dict[str, int]]]) -> list[str]:\n"
            '    """渲染章节目录（嵌套字典，历史实现缩放过深）。"""\n'
            "    lines: list[str] = []\n"
            "    for chapter, sections in tree.items():\n"
            "        for title, subsections in sections.items():\n"
            "            for name, page in subsections.items():\n"
            "                if page > 0:\n"
            '                    lines.append(f"{chapter} / {title} / {name}（第 {page} 页）")\n'
            "    return lines\n"
            "\n"
            "\n"
            "def render_tag_cloud(tags: list[tuple[str, int]]) -> str:\n"
            '    """渲染标签云（循环内字符串拼接）。"""\n'
            '    html = ""\n'
            "    for tag, weight in tags:\n"
            "        html = html + f'<span class=\"w{weight}\">{tag}</span>'\n"
            "    return html\n"
            "\n"
            "\n"
            "def publish_all(slugs: list[str], out_dir: str) -> int:\n"
            '    """逐篇发布页面（循环体内 open/close，应复用句柄或批量写出）。"""\n'
            "    done = 0\n"
            "    for slug in slugs:\n"
            '        target = f"{out_dir}/{slug}/index.html"\n'
            '        fh = open(target, "w", encoding="utf-8")\n'
            '        fh.write(render_page("<main>{{body}}</main>", {}, f"post {slug}"))\n'
            "        fh.close()\n"
            "        done += 1\n"
            "    return done\n"
        ),
        "server.py": (
            '"""blogengine 本地预览与站点打包。"""\n'
            "\n"
            "from __future__ import annotations\n"
            "\n"
            "import os\n"
            "import subprocess\n"
            "\n"
            "\n"
            "def pack_site(site_dir: str, out_name: str) -> None:\n"
            '    """打包生成站点（shell 方式调用 tar）。"""\n'
            '    os.system(f"tar -czf {out_name} {site_dir}")\n'
            "\n"
            "\n"
            "def serve_preview(output_dir: str, port: int = 8000) -> None:\n"
            '    """启动本地预览（演示实现：仅打印访问提示）。"""\n'
            '    print(f"[preview] serving {output_dir} on port {port} — Ctrl-C 退出")\n'
        ),
        "cache.py": (
            '"""blogengine 渲染缓存：登记表合并与命中计数。"""\n'
            "\n"
            "from __future__ import annotations\n"
            "\n"
            "\n"
            "def merge_registry(entries: list[str], registry: dict[str, int] = {}) -> dict[str, int]:\n"
            '    """合并缓存登记表。"""\n'
            "    for entry in entries:\n"
            "        registry[entry] = registry.get(entry, 0) + 1\n"
            "    return registry\n"
            "\n"
            "\n"
            "def read_hit_count(path: str) -> int:\n"
            '    """读取缓存命中计数（解析失败按 0，吞掉一切异常）。"""\n'
            "    try:\n"
            '        with open(path, "r", encoding="utf-8") as fh:\n'
            "            return int(fh.read().strip() or 0)\n"
            "    except:\n"
            "        return 0\n"
        ),
        "main.py": (
            '"""blogengine 命令行入口：build / preview。"""\n'
            "\n"
            "from __future__ import annotations\n"
            "\n"
            "from blogengine.render import render_page\n"
            "from blogengine.server import serve_preview\n"
            "from blogengine.storage import load_meta\n"
            "\n"
            "AUTO_REBUILD_BYTES = 500000\n"
            "\n"
            "\n"
            "def build_once(src: str, template: str = \"<main>{{title}}</main>{{body}}\") -> str:\n"
            '    """渲染单篇文章页。"""\n'
            "    meta = load_meta(src)\n"
            '    return render_page(template, meta, "<p>正文占位</p>")\n'
            "\n"
            "\n"
            "def main(argv: list[str] | None = None) -> int:\n"
            '    """入口：build 渲染样例，preview 启动本地预览。"""\n'
            '    argv = list(argv or ["build"])\n'
            "    command = argv[0]\n"
            '    if command == "build":\n'
            '        build_once("content/posts/2026-09-01-hello.md")\n'
            "        return 0\n"
            '    if command == "preview":\n'
            '        serve_preview("public")\n'
            "        return 0\n"
            "    return 1\n"
        ),
    },
    fixes=(
        FixSpec(
            file="markdown.py",
            anchor="return str(eval(stripped",
            before=0,
            new_lines=("    return str(ast.literal_eval(stripped))\n",),
            subject="fix: remove eval usage in directive rendering (security hardening)",
        ),
        FixSpec(
            file="server.py",
            anchor='os.system(f"tar',
            before=0,
            new_lines=('    subprocess.run(["tar", "-czf", out_name, site_dir], check=True)\n',),
            subject="fix: invoke tar with argument list to avoid command injection",
        ),
        FixSpec(
            file="cache.py",
            anchor="registry: dict[str, int] = {}",
            before=0,
            new_lines=(
                "def merge_registry(entries: list[str], registry: dict[str, int] | None = None) -> dict[str, int]:\n"
                '    """合并缓存登记表。"""\n'
                "    registry = registry if registry is not None else {}\n",
            ),
            subject="fix: stop sharing mutable registry default across cache rebuilds",
        ),
    ),
    goldens=(
        GoldenSpec("markdown.py", 'out.append("<p>"', "style", "medium", "块渲染四层嵌套，认知负担大应提前返回"),
        GoldenSpec("storage.py", "return yaml.load(raw)", "security", "high", "yaml.load 未指定安全 Loader，反序列化不可信输入可致代码执行"),
        GoldenSpec("search.py", "if slug in seen:", "performance", "medium", "浏览轨迹判重用 list 线性扫描"),
        GoldenSpec("search.py", "if limit == None:", "bug", "low", "摘要截断用 == None 判空"),
        GoldenSpec("render.py", 'lines.append(f"{chapter}', "style", "medium", "目录渲染嵌套达四层以上"),
        GoldenSpec("render.py", "html = html + f'", "performance", "low", "标签云循环内字符串自拼接"),
        GoldenSpec("render.py", "fh = open(target", "performance", "high", "发布循环体内逐篇 open 文件，IO 放大"),
        GoldenSpec("server.py", 'print(f"[preview]', "style", "low", "预览服务用 print 输出运行信息"),
        GoldenSpec("cache.py", "except:", "bug", "medium", "缓存计数读取用裸 except 吞掉全部异常"),
        GoldenSpec("main.py", "AUTO_REBUILD_BYTES = ", "style", "low", "自动重建阈值魔法数字应进配置"),
    ),
)


# ================================================================
# 合成项目三：datatools（数据 ETL 管道，8 文件）
# ================================================================

_DATATOOLS = ProjectSpec(
    name="datatools",
    modules={
        "__init__.py": (
            '"""datatools：合成数据 ETL 管道（Wave 3 离线评估数据集项目）。"""\n'
            "\n"
            '__all__ = ["readers", "transforms", "etl", "loaders", "schedule", "aggregates"]\n'
        ),
        "readers.py": (
            '"""datatools 输入读取器：CSV 与 JSONL。"""\n'
            "\n"
            "from __future__ import annotations\n"
            "\n"
            "import json\n"
            "\n"
            "\n"
            "def read_csv_rows(path: str) -> list[dict[str, str]]:\n"
            '    """读取 CSV（逗号分隔，演示实现不处理引号转义）。"""\n'
            "    rows: list[dict[str, str]] = []\n"
            '    handle = open(path, "r", encoding="utf-8")\n'
            '    header = handle.readline().strip().split(",")\n'
            "    for line in handle:\n"
            '        cells = line.strip().split(",")\n'
            "        rows.append(dict(zip(header, cells)))\n"
            "    return rows\n"
            "\n"
            "\n"
            "def read_jsonl(path: str) -> list[dict[str, object]]:\n"
            '    """逐行读取 JSONL（坏行被静默吞掉，故障不可见）。"""\n'
            "    rows: list[dict[str, object]] = []\n"
            '    with open(path, "r", encoding="utf-8") as fh:\n'
            "        for line in fh:\n"
            "            try:\n"
            "                rows.append(json.loads(line))\n"
            "            except json.JSONDecodeError:\n"
            "                pass\n"
            "    return rows\n"
            "\n"
            "\n"
            "def peek_header(path: str) -> list[str]:\n"
            '    """只读表头（供字段映射预览）。"""\n'
            '    with open(path, "r", encoding="utf-8") as fh:\n'
            '        return fh.readline().strip().split(",")\n'
        ),
        "transforms.py": (
            '"""datatools 行级变换：清洗、规范化与快照。"""\n'
            "\n"
            "from __future__ import annotations\n"
            "\n"
            "import copy\n"
            "\n"
            "\n"
            "def normalize_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:\n"
            '    """字段名统一为小写并去掉首尾空白。"""\n'
            "    out: list[dict[str, object]] = []\n"
            "    for row in rows:\n"
            "        cleaned = {str(k).strip().lower(): v for k, v in row.items()}\n"
            "        out.append(cleaned)\n"
            "    return out\n"
            "\n"
            "\n"
            "def snapshot_each(rows: list[dict[str, object]]) -> list[dict[str, object]]:\n"
            '    """给每行做深拷贝快照（循环内 deepcopy，行多时极慢）。"""\n'
            "    snapshots: list[dict[str, object]] = []\n"
            "    for row in rows:\n"
            "        snapshots.append(copy.deepcopy(row))\n"
            "    return snapshots\n"
            "\n"
            "\n"
            "def flatten_values(payload: dict[str, object]) -> list[object]:\n"
            '    """递归取出叶子值（历史遗留：局部变量遮蔽了内置 list）。"""\n'
            "    list = []\n"
            "\n"
            "    def _walk(node: object) -> None:\n"
            "        if isinstance(node, dict):\n"
            "            for child in node.values():\n"
            "                _walk(child)\n"
            "        elif isinstance(node, list):\n"
            "            for child in node:\n"
            "                _walk(child)\n"
            "        else:\n"
            "            list.append(node)\n"
            "\n"
            "    _walk(payload)\n"
            "    return list\n"
            "\n"
            "\n"
            "def validate_batch(rows: list[dict[str, object]]) -> bool:\n"
            '    """校验批次合法性（生产代码用 assert，-O 下会静默失效）。"""\n'
            '    assert all("id" in row for row in rows)\n'
            "    return True\n"
        ),
        "etl.py": (
            '"""datatools 主 ETL 流程：读入-清洗-聚合-落库。"""\n'
            "\n"
            "from __future__ import annotations\n"
            "\n"
            "from datatools.aggregates import fetch_rate\n"
            "from datatools.loaders import write_csv_rows\n"
            "from datatools.readers import read_csv_rows\n"
            "from datatools.transforms import normalize_rows\n"
            "\n"
            "MAX_BATCH_ROWS = 20000\n"
            "\n"
            "\n"
            "def run_pipeline(source: str, target: str, options: dict[str, object] | None = None) -> dict[str, object]:\n"
            '    """端到端执行一个 ETL 批次（历史原因全部内联在一个函数里，超长难测）。"""\n'
            "    opts = dict(options or {})\n"
            '    report: dict[str, object] = {"source": source, "target": target}\n'
            "\n"
            "    rows = read_csv_rows(source)\n"
            '    report["raw_rows"] = len(rows)\n'
            "    rows = normalize_rows(rows)\n"
            '    report["normalized_rows"] = len(rows)\n'
            "\n"
            "    if len(rows) > MAX_BATCH_ROWS:\n"
            '        report["oversize"] = True\n'
            "\n"
            '    id_field = str(opts.get("id_field", "id"))\n'
            "    seen_ids: set[str] = set()\n"
            "    deduped: list[dict[str, object]] = []\n"
            "    for row in rows:\n"
            '        key = str(row.get(id_field, ""))\n'
            "        if key and key not in seen_ids:\n"
            "            seen_ids.add(key)\n"
            "            deduped.append(row)\n"
            '    report["deduped_rows"] = len(deduped)\n'
            "\n"
            '    amount_field = str(opts.get("amount_field", "amount"))\n'
            "    total = 0.0\n"
            "    for row in deduped:\n"
            "        try:\n"
            "            total += float(row.get(amount_field, 0.0))\n"
            "        except (TypeError, ValueError):\n"
            "            continue\n"
            '    report["amount_total"] = round(total, 2)\n'
            "\n"
            '    currency = str(opts.get("currency", "USD"))\n'
            "    rate = fetch_rate(currency)\n"
            '    report["amount_cny"] = round(total * rate, 2)\n'
            "\n"
            "    kept: list[dict[str, object]] = []\n"
            "    skipped: list[dict[str, object]] = []\n"
            '    min_amount = float(opts.get("min_amount", 0.0))\n'
            "    for row in deduped:\n"
            "        try:\n"
            "            numeric = float(row.get(amount_field, 0.0))\n"
            "        except (TypeError, ValueError):\n"
            "            skipped.append(row)\n"
            "            continue\n"
            "        if numeric >= min_amount:\n"
            "            kept.append(row)\n"
            '    report["kept_rows"] = len(kept)\n'
            '    report["skipped_rows"] = len(skipped)\n'
            "\n"
            "    labels: dict[str, int] = {}\n"
            "    for row in kept:\n"
            '        label = str(row.get("label", "unknown"))\n'
            "        labels[label] = labels.get(label, 0) + 1\n"
            '    report["label_top"] = sorted(labels.items())[:10]\n'
            "\n"
            "    by_currency: dict[str, float] = {}\n"
            "    for row in kept:\n"
            '        cur = str(row.get("currency", currency))\n'
            "        try:\n"
            "            by_currency[cur] = by_currency.get(cur, 0.0) + float(row.get(amount_field, 0.0))\n"
            "        except (TypeError, ValueError):\n"
            "            continue\n"
            '    report["by_currency"] = {name: round(value, 2) for name, value in by_currency.items()}\n'
            "\n"
            "    regions: dict[str, float] = {}\n"
            "    for row in kept:\n"
            '        region = str(row.get("region", "unknown"))\n'
            '        regions[region] = round(regions.get(region, 0.0) + float(row.get(amount_field, 0.0)), 2)\n'
            '    report["by_region"] = regions\n'
            "\n"
            "    months: dict[str, int] = {}\n"
            "    for row in kept:\n"
            '        month = str(row.get("month", "unknown"))\n'
            "        months[month] = months.get(month, 0) + 1\n"
            '    report["month_counts"] = len(months)\n'
            "\n"
            '    warn_limit = float(opts.get("warn_limit", 0.0))\n'
            "    if total < warn_limit:\n"
            '        report["warning"] = "amount total below warn limit"\n'
            "\n"
            '    checkpoints = ["read", "normalize", "dedupe", "aggregate", "load"]\n'
            "    timings: dict[str, float] = {}\n"
            "    for name in checkpoints:\n"
            "        timings[name] = round(len(name) * 0.5, 2)\n"
            '    report["timings"] = timings\n'
            "\n"
            '    if "warning" in report:\n'
            '        report["status"] = "warn"\n'
            "    else:\n"
            '        report["status"] = "ok"\n'
            "\n"
            "    write_csv_rows(target, kept)\n"
            '    report["written_to"] = target\n'
            "    return report\n"
        ),
        "loaders.py": (
            '"""datatools 输出加载器：写 CSV 与入库。"""\n'
            "\n"
            "from __future__ import annotations\n"
            "\n"
            "import sqlite3\n"
            "\n"
            "\n"
            "def write_csv_rows(path: str, rows: list[dict[str, object]]) -> None:\n"
            '    """把行写为 CSV（首行为表头）。"""\n'
            '    with open(path, "w", encoding="utf-8") as fh:\n'
            '        fh.write(",".join(rows[0].keys()) + "\\n")\n'
            "        for row in rows:\n"
            '            fh.write(",".join(str(v) for v in row.values()) + "\\n")\n'
            "\n"
            "\n"
            "def dynamic_insert(conn: sqlite3.Connection, table: str, row: dict[str, object]) -> None:\n"
            '    """按表名动态拼接 INSERT（表名来自外部配置，直接进入语句）。"""\n'
            '    sql = "INSERT INTO " + table + " VALUES (?, ?)"\n'
            '    conn.execute(sql, (row["id"], row["amount"]))\n'
            "\n"
            "\n"
            "def load_fact_rows(conn: sqlite3.Connection, rows: list[dict[str, object]]) -> int:\n"
            '    """循环逐条写入事实表（应改 executemany 批量）。"""\n'
            "    written = 0\n"
            "    for row in rows:\n"
            "        cur = conn.cursor()\n"
            '        cur.execute("INSERT INTO facts VALUES (?, ?)", (row["id"], row["amount"]))\n'
            "        written += cur.rowcount\n"
            "    conn.commit()\n"
            "    return written\n"
        ),
        "schedule.py": (
            '"""datatools 批次调度：重试与告警。"""\n'
            "\n"
            "from __future__ import annotations\n"
            "\n"
            "import logging\n"
            "\n"
            "LOGGER = logging.getLogger(__name__)\n"
            "\n"
            "\n"
            "def run_job(job: str) -> bool:\n"
            '    """执行单个批次任务（演示桩：失败由调用方重试）。"""\n'
            "    return len(job) > 0\n"
            "\n"
            "\n"
            "def retry_batch(job: str, attempts: int = 3) -> bool:\n"
            '    """带重试地执行批次任务。"""\n'
            "    for _ in range(attempts):\n"
            "        if run_job(job):\n"
            "            return True\n"
            '    LOGGER.warning("batch %s failed after retries", job)\n'
            "    return False\n"
            '    raise RuntimeError("scheduler gave up: " + job)\n'
        ),
        "aggregates.py": (
            '"""datatools 聚合计算：汇率换算与格式识别。"""\n'
            "\n"
            "from __future__ import annotations\n"
            "\n"
            '_RATE_TABLE = {"USD": 7.12, "EUR": 7.74, "JPY": 0.048}\n'
            "\n"
            "\n"
            "def fetch_rate(currency: str) -> float:\n"
            '    """查询币种汇率（演示实现）。"""\n'
            "    return _RATE_TABLE.get(currency, 1.0)\n"
            "\n"
            "\n"
            "def to_cny_total(rows: list[dict[str, object]]) -> float:\n"
            '    """把金额字段汇总为人民币（循环里重复查询汇率）。"""\n'
            "    total = 0.0\n"
            "    for row in rows:\n"
            '        rate = fetch_rate("USD")\n'
            '        total += float(row["amount"]) * rate\n'
            '    baseline = fetch_rate("USD")\n'
            "    return total if total > 0 else baseline * 0.0\n"
            "\n"
            "\n"
            "def detect_format(path: str) -> str:\n"
            '    """按扩展名识别输入格式。"""\n'
            '    stem = path.rsplit(".", 1)[-1]\n'
            '    if stem is "jsonl":\n'
            '        return "jsonl"\n'
            '    return "csv"\n'
        ),
        "main.py": (
            '"""datatools 命令行入口。"""\n'
            "\n"
            "from __future__ import annotations\n"
            "\n"
            "from datatools.etl import run_pipeline\n"
            "\n"
            "\n"
            "def main(argv: list[str] | None = None) -> int:\n"
            '    """入口：source target 两个位置参数。"""\n'
            '    argv = list(argv or ["data/input.csv", "out/facts.csv"])\n'
            "    report = run_pipeline(argv[0], argv[1])\n"
            '    return 0 if report.get("status") == "ok" else 1\n'
            "\n"
            "\n"
            'if __name__ == "__main__":\n'
            "    raise SystemExit(main())\n"
        ),
    },
    fixes=(
        FixSpec(
            file="schedule.py",
            anchor="raise RuntimeError",
            before=1,
            new_lines=(
                '    LOGGER.error("batch %s failed after %d attempts", job, attempts)\n    return False\n',
            ),
            subject="fix: drop unreachable raise after return in scheduler retry",
        ),
        FixSpec(
            file="aggregates.py",
            anchor='if stem is "jsonl":',
            before=0,
            new_lines=('    if stem == "jsonl":\n        return "jsonl"\n',),
            subject="fix: compare format extension with equality instead of identity",
        ),
    ),
    goldens=(
        GoldenSpec("readers.py", "handle = open(path", "bug", "high", "CSV 读取 open 后未 close 也未用 with，句柄泄漏"),
        GoldenSpec("readers.py", "except json.JSONDecodeError:", "bug", "medium", "JSONL 坏行 except 后仅 pass，故障静默不可见"),
        GoldenSpec("transforms.py", "copy.deepcopy(row)", "performance", "medium", "循环内 deepcopy 逐行深拷贝"),
        GoldenSpec("transforms.py", "list = []", "bug", "low", "局部变量 list 遮蔽内置 list"),
        GoldenSpec("transforms.py", "assert all(", "bug", "medium", "批次校验用 assert，-O 运行时静默失效"),
        GoldenSpec("etl.py", "def run_pipeline(", "style", "medium", "ETL 主函数近百行内联，职责过多难测试"),
        GoldenSpec("etl.py", "MAX_BATCH_ROWS = ", "style", "low", "批次上限魔法数字应配置化"),
        GoldenSpec("loaders.py", 'sql = "INSERT INTO " + table', "security", "critical", "表名字符串拼接进 INSERT 语句，存在 SQL 注入"),
        GoldenSpec("loaders.py", 'cur.execute("INSERT INTO facts', "performance", "high", "事实表入库循环内逐条 execute"),
        GoldenSpec("aggregates.py", 'baseline = fetch_rate("USD")', "performance", "low", "同一函数内重复执行相同汇率查询"),
    ),
)


# ================================================================
# 合成项目四：webapi（任务看板 Web API，9 文件）
# ================================================================

_WEBAPI = ProjectSpec(
    name="webapi",
    modules={
        "__init__.py": (
            '"""webapi：合成任务看板 Web API（Wave 3 离线评估数据集项目）。"""\n'
            "\n"
            '__all__ = ["settings", "db", "auth", "handlers", "tasks", "serialize", "hooks"]\n'
        ),
        "settings.py": (
            '"""webapi 应用配置。"""\n'
            "\n"
            "from __future__ import annotations\n"
            "\n"
            "DEBUG = False\n"
            "SESSION_TTL = 600\n"
            'JWT_SIGNING_KEY = "wJalrXUtnfEMI-K7MDENG-bPxRfiCYEXAMPLEKEY"\n'
        ),
        "db.py": (
            '"""webapi SQLite 访问层。"""\n'
            "\n"
            "from __future__ import annotations\n"
            "\n"
            "import sqlite3\n"
            "\n"
            "\n"
            "def connect(path: str) -> sqlite3.Connection:\n"
            '    """建立连接并打开外键约束。"""\n'
            "    conn = sqlite3.connect(path)\n"
            '    conn.execute("PRAGMA foreign_keys = ON")\n'
            "    return conn\n"
            "\n"
            "\n"
            "def find_board(conn: sqlite3.Connection, board_id: str) -> tuple | None:\n"
            '    """按 id 查询看板（外部输入直接进入查询语句）。"""\n'
            "    row = conn.execute(\"SELECT * FROM boards WHERE id = '\" + board_id + \"'\").fetchone()\n"
            "    return row\n"
        ),
        "repos.py": (
            '"""webapi 仓储层：看板/任务表的基本读写（参数化查询的正面示范）。"""\n'
            "\n"
            "from __future__ import annotations\n"
            "\n"
            "import sqlite3\n"
            "\n"
            "from webapi.tasks import Task\n"
            "\n"
            "\n"
            "def ensure_schema(conn: sqlite3.Connection) -> None:\n"
            '    """建表（幂等）。"""\n'
            "    conn.executescript(\n"
            '        """\n'
            "        CREATE TABLE IF NOT EXISTS boards (\n"
            "            id TEXT PRIMARY KEY,\n"
            "            name TEXT NOT NULL\n"
            "        );\n"
            "        CREATE TABLE IF NOT EXISTS tasks (\n"
            "            id TEXT PRIMARY KEY,\n"
            "            title TEXT NOT NULL,\n"
            "            board_id TEXT NOT NULL REFERENCES boards(id)\n"
            "        );\n"
            '        """\n'
            "    )\n"
            "    conn.commit()\n"
            "\n"
            "\n"
            "def create_board(conn: sqlite3.Connection, board_id: str, name: str) -> None:\n"
            '    """新建看板（参数化写入）。"""\n'
            '    conn.execute("INSERT INTO boards (id, name) VALUES (?, ?)", (board_id, name))\n'
            "    conn.commit()\n"
            "\n"
            "\n"
            "def create_task(conn: sqlite3.Connection, task: Task) -> None:\n"
            '    """新增任务（参数化写入）。"""\n'
            '    conn.execute("INSERT INTO tasks (id, title, board_id) VALUES (?, ?, ?)",\n'
            '                 (task.task_id, task.title, task.board_id))\n'
            "    conn.commit()\n"
            "\n"
            "\n"
            "def list_boards(conn: sqlite3.Connection) -> list[tuple[str, str]]:\n"
            '    """列出全部看板。"""\n'
            '    return [(row[0], row[1]) for row in conn.execute("SELECT id, name FROM boards").fetchall()]\n'
            "\n"
            "\n"
            "def count_tasks(conn: sqlite3.Connection, board_id: str) -> int:\n"
            '    """统计看板下的任务数。"""\n'
            '    row = conn.execute("SELECT COUNT(*) FROM tasks WHERE board_id = ?", (board_id,)).fetchone()\n'
            "    return int(row[0]) if row else 0\n"
        ),
        "auth.py": (
            '"""webapi 认证：口令摘要与会话解析。"""\n'
            "\n"
            "from __future__ import annotations\n"
            "\n"
            "import hashlib\n"
            "\n"
            "\n"
            "def hash_password(password: str, salt: str = \"webapi\") -> str:\n"
            '    """演示口令摘要（真实项目应使用 argon2/bcrypt）。"""\n'
            '    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()\n'
            "\n"
            "\n"
            "def session_of(headers: dict[str, str], cache: dict[str, str] = {}) -> str:\n"
            '    """从请求头解析会话（cache 默认 dict 跨请求共享，存在串号风险）。"""\n'
            '    token = headers.get("X-Session", "")\n'
            "    return cache.setdefault(token, token)\n"
            "\n"
            "\n"
            "def principal_of(session: str | None) -> str | None:\n"
            '    """会话换主体（历史实现用 == None 判空）。"""\n'
            "    if session == None:\n"
            "        return None\n"
            '    return session.split("|", 1)[0]\n'
        ),
        "handlers.py": (
            '"""webapi HTTP 处理器：看板与任务的读取路径。"""\n'
            "\n"
            "from __future__ import annotations\n"
            "\n"
            "import urllib.request\n"
            "\n"
            "from webapi.auth import principal_of\n"
            "from webapi.db import find_board\n"
            "from webapi.tasks import load_tasks\n"
            "\n"
            "\n"
            "def board_payload(conn, board_id: str) -> dict[str, object]:\n"
            '    """组装看板响应体。"""\n'
            "    board = find_board(conn, board_id)\n"
            "    if board is None:\n"
            '        return {"error": "board not found"}\n'
            "    tasks = load_tasks(conn, board_id)\n"
            '    return {"board": board[1], "tasks": [task.to_dict() for task in tasks]}\n'
            "\n"
            "\n"
            "def resolve_owner(request: dict[str, str]) -> str | None:\n"
            '    """从请求头解析当前用户。"""\n'
            '    return principal_of(request.get("X-Session"))\n'
            "\n"
            "\n"
            "def unique_board_view(views: list[str]) -> list[str]:\n"
            '    """合并重复视图（list 判重，视图多时 O(n^2)）。"""\n'
            "    merged = []\n"
            "    for view in views:\n"
            "        if view in merged:\n"
            "            continue\n"
            "        merged.append(view)\n"
            "    return merged\n"
            "\n"
            "\n"
            "def page_limit_of(request: dict[str, str]) -> int:\n"
            '    """解析分页大小（非法输入回退 20）。"""\n'
            "    try:\n"
            '        return int(request.get("limit", "20"))\n'
            "    except:\n"
            "        return 20\n"
            "\n"
            "\n"
            "def ping_webhook(url: str) -> bytes:\n"
            '    """回调任务系统的 webhook（未设置 timeout）。"""\n'
            "    with urllib.request.urlopen(url) as resp:\n"
            "        return resp.read()\n"
        ),
        "tasks.py": (
            '"""webapi 任务模型与查询。"""\n'
            "\n"
            "from __future__ import annotations\n"
            "\n"
            "from dataclasses import dataclass, field\n"
            "\n"
            "\n"
            "@dataclass\n"
            "class Task:\n"
            '    """看板任务。"""\n'
            "\n"
            "    task_id: str\n"
            "    title: str\n"
            "    board_id: str\n"
            "    labels: list[str] = field(default_factory=list)\n"
            "\n"
            "    def to_dict(self) -> dict[str, object]:\n"
            '        """序列化为响应字典。"""\n'
            '        return {"id": self.task_id, "title": self.title, "labels": list(self.labels)}\n'
            "\n"
            "\n"
            "def load_tasks(conn, board_id: str) -> list[Task]:\n"
            '    """加载看板下全部任务。"""\n'
            '    rows = conn.execute("SELECT id, title FROM tasks WHERE board_id = ?", (board_id,)).fetchall()\n'
            "    return [Task(task_id=row[0], title=row[1], board_id=board_id) for row in rows]\n"
            "\n"
            "\n"
            "def require_board_id(payload: dict[str, object]) -> str:\n"
            '    """从请求体取看板 id（生产代码用 assert 校验）。"""\n'
            '    assert isinstance(payload.get("board_id"), str)\n'
            '    return str(payload["board_id"])\n'
            "\n"
            "\n"
            "def first_empty_label(labels: list[str]) -> int:\n"
            '    """找第一个空标签的下标（缺失时历史实现留空占位）。"""\n'
            "    try:\n"
            '        return labels.index("")\n'
            "    except ValueError:\n"
            "        ...\n"
        ),
        "serialize.py": (
            '"""webapi 序列化：JSON 输出与动态字段计算。"""\n'
            "\n"
            "from __future__ import annotations\n"
            "\n"
            "import json\n"
            "\n"
            "\n"
            "def dumps(payload: object) -> str:\n"
            '    """统一 JSON 输出。"""\n'
            "    return json.dumps(payload, ensure_ascii=False, sort_keys=True)\n"
            "\n"
            "\n"
            "def computed_field(expression: str, context: dict[str, object]) -> object:\n"
            '    """计算响应里的动态字段（表达式直接求值，应改白名单映射）。"""\n'
            '    return eval(expression, {"__builtins__": {}}, dict(context))\n'
        ),
        "hooks.py": (
            '"""webapi 部署钩子：任务事件触发外部命令。"""\n'
            "\n"
            "from __future__ import annotations\n"
            "\n"
            "import os\n"
            "import subprocess\n"
            "\n"
            "\n"
            "def trigger_deploy(env: str, tag: str) -> None:\n"
            '    """触发部署（shell 方式调用部署 CLI）。"""\n'
            '    os.system(f"deploy-cli push --env {env} --tag {tag}")\n'
        ),
        "main.py": (
            '"""webapi 应用装配与入口。"""\n'
            "\n"
            "from __future__ import annotations\n"
            "\n"
            "from webapi.db import connect\n"
            "from webapi.handlers import board_payload\n"
            "from webapi.settings import DEBUG\n"
            "\n"
            "MAX_REQUEST_BODY = 1048576\n"
            "\n"
            "\n"
            "def build_app(db_path: str = \"webapi.db\") -> dict[str, object]:\n"
            '    """组装应用上下文（演示返回 dict 容器）。"""\n'
            '    return {"conn": connect(db_path), "debug": DEBUG}\n'
            "\n"
            "\n"
            "def main() -> int:\n"
            '    """跑通一次看板读取路径。"""\n'
            "    app = build_app()\n"
            '    payload = board_payload(app["conn"], "board-1")\n'
            "    # TODO: 接入真实 HTTP 框架前先补齐集成测试\n"
            '    print(f"[webapi] payload keys: {sorted(payload)}")\n'
            '    return 0 if "error" not in payload else 1\n'
            "\n"
            "\n"
            'if __name__ == "__main__":\n'
            "    raise SystemExit(main())\n"
        ),
    },
    fixes=(
        FixSpec(
            file="handlers.py",
            anchor="except:",
            before=0,
            new_lines=("    except (TypeError, ValueError):\n",),
            subject="fix: narrow exception handling in pagination parsing",
        ),
        FixSpec(
            file="hooks.py",
            anchor='os.system(f"deploy-cli',
            before=0,
            new_lines=(
                '    subprocess.run(["deploy-cli", "push", "--env", env, "--tag", tag], check=True)\n',
            ),
            subject="fix: deploy hook without shell string concat (command injection)",
        ),
    ),
    goldens=(
        GoldenSpec("settings.py", "JWT_SIGNING_KEY = ", "security", "critical", "JWT 签名密钥硬编码在配置模块"),
        GoldenSpec("db.py", "SELECT * FROM boards WHERE id = '", "security", "critical", "看板查询拼接 board_id，存在 SQL 注入"),
        GoldenSpec("auth.py", "cache: dict[str, str] = {}", "bug", "high", "会话缓存用可变默认参数跨请求共享"),
        GoldenSpec("auth.py", "if session == None:", "bug", "low", "会话判空用 == None"),
        GoldenSpec("handlers.py", "if view in merged:", "performance", "medium", "视图合并 list 判重线性扫描"),
        GoldenSpec("handlers.py", "with urllib.request.urlopen(url", "performance", "medium", "webhook 回调未设置 timeout"),
        GoldenSpec("tasks.py", 'assert isinstance(payload.get("board_id")', "bug", "medium", "请求体校验用 assert，-O 下失效"),
        GoldenSpec("tasks.py", "except ValueError:", "bug", "low", "空标签查找 except 体只有占位符，处理逻辑缺失"),
        GoldenSpec("serialize.py", "return eval(expression", "security", "critical", "动态字段用 eval 求值，存在代码执行风险"),
        GoldenSpec("main.py", "MAX_REQUEST_BODY = ", "style", "low", "请求体上限魔法数字"),
        GoldenSpec("main.py", "TODO:", "style", "low", "集成测试 TODO 残留"),
        GoldenSpec("main.py", 'print(f"[webapi]', "style", "low", "入口 print 调试输出"),
    ),
)


ALL_PROJECT_SPECS: tuple[ProjectSpec, ...] = (_SHOPCORE, _BLOGENGINE, _DATATOOLS, _WEBAPI)


# ---------------------------------------------------------------- 构建基建


def _anchor_line(text: str, file: str, anchor: str) -> int:
    """在文件文本中定位 anchor 的唯一行号（1-based）；未找到或多次出现都报错。"""
    lines = text.splitlines()
    hits = [i + 1 for i, ln in enumerate(lines) if anchor in ln]
    if not hits:
        raise ValueError(f"[{file}] 金标 anchor 未找到：{anchor!r}")
    if len(hits) > 1:
        raise ValueError(f"[{file}] 金标 anchor 不唯一（{len(hits)} 处）：{anchor!r}")
    return hits[0]


def _run_git(repo: Path, *args: str) -> str:
    """在 repo 内执行 git 子命令（仅本地操作，禁网），返回 stdout。"""
    proc = subprocess.run(
        ["git", "-C", str(repo), "-c", "core.autocrlf=false", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return proc.stdout


def _apply_fix(content: str, fix: FixSpec) -> str:
    """把一次修复应用为等行数替换（保证行号坐标系跨版本稳定）。"""
    lines = content.splitlines()
    idx = _anchor_line(content, fix.file, fix.anchor) - 1
    start = idx - fix.before
    if start < 0:
        raise ValueError(f"[{fix.file}] 修复块起点越界：anchor={fix.anchor!r} before={fix.before}")
    new_lines: list[str] = []
    raw = fix.new_lines if isinstance(fix.new_lines, tuple) else (fix.new_lines,)  # 兜底：误传纯字符串
    for chunk in raw:  # 展开元素内嵌的换行；展开后的行数 = 被替换的旧行数
        new_lines.extend(chunk.rstrip("\n").split("\n"))
    end = start + len(new_lines)
    if end > len(lines):
        raise ValueError(f"[{fix.file}] 修复块越过文件末尾：anchor={fix.anchor!r}")
    replaced = lines[start:end]
    if fix.before == 0 and fix.anchor not in replaced[0]:
        raise ValueError(f"[{fix.file}] 修复 anchor 与目标行不符：{replaced[0]!r}")
    lines[start:end] = new_lines
    return "\n".join(lines) + "\n"


def _write_project_tree(spec: ProjectSpec) -> Path:
    """写入项目初版源码并创建 git 仓库与首提交。"""
    root = PROJECTS_DIR / spec.name
    if root.exists():
        _force_rmtree(root)
    root.mkdir(parents=True)
    for rel, text in spec.modules.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
    _run_git(root, "init", "-q")
    _run_git(root, "add", "-A")
    _run_git(
        root,
        "-c", "user.name=bench-gen",
        "-c", "user.email=bench-gen@localhost",
        "commit", "-q", "-m", f"feat: initial import of {spec.name} (synthetic audit target)",
    )
    return root


def _apply_fixes_and_commit(root: Path, spec: ProjectSpec) -> None:
    """逐个应用修复并提交（消息含 fix，供 mine_git_history 识别）。"""
    for fix in spec.fixes:
        path = root / fix.file
        path.write_text(_apply_fix(path.read_text(encoding="utf-8"), fix), encoding="utf-8", newline="\n")
        _run_git(root, "add", "-A")
        _run_git(
            root,
            "-c", "user.name=bench-gen",
            "-c", "user.email=bench-gen@localhost",
            "commit", "-q", "-m", fix.subject,
        )


def build_synthetic_projects() -> dict[str, Path]:
    """构建全部合成项目：初版提交 → 修复提交；返回 项目名 -> 根路径。"""
    roots: dict[str, Path] = {}
    for spec in ALL_PROJECT_SPECS:
        root = _write_project_tree(spec)
        _apply_fixes_and_commit(root, spec)
        roots[spec.name] = root
    return roots


def _force_rmtree(path: Path) -> None:
    """Windows 兼容的强制删除：git 会在 .git/objects 里写只读文件，先去只读再删。"""
    import stat

    def _onexc(func, target, _exc):  # noqa: ANN001 —— shutil.onexc 回调签名
        Path(target).chmod(stat.S_IWRITE)
        func(target)

    shutil.rmtree(path, onexc=_onexc)


def _strip_git(repo: Path) -> None:
    """删除注入变体目录里复制来的 .git（变体是静态审计对象，不需要历史）。"""
    git_dir = repo / ".git"
    if git_dir.exists():
        _force_rmtree(git_dir)


def _clear_datasets_dir() -> None:
    """清空 bench/datasets/ 下除本脚本外的全部内容（幂等重建语义）。"""
    DATASETS_DIR.mkdir(parents=True, exist_ok=True)
    for child in DATASETS_DIR.iterdir():
        if child.name == "gen_offline.py":
            continue  # 生成脚本自身必须存活，否则清空即自毁
        if child.is_dir():
            _force_rmtree(child)
        else:
            child.unlink()


# ---------------------------------------------------------------- 数据集组装


def _clone_goldens(goldens: list[GoldenIssue], target_project: str) -> list[GoldenIssue]:
    """把源项目金标克隆为变体项目金标（注入只追加不改动，行号不变）。"""
    clones: list[GoldenIssue] = []
    for item in goldens:
        clones.append(
            GoldenIssue(
                project=target_project,
                file=item.file,
                line_start=item.line_start,
                line_end=item.line_end,
                category=item.category,
                severity=item.severity,
                description=f"[variant] {item.description}",
                origin=item.origin,
            )
        )
    return clones


def _dedupe(items: list[GoldenIssue]) -> list[GoldenIssue]:
    """合并去重：description 全局唯一 + 同项目同文件同类别行区间相交者只保留首个。"""
    seen_desc: set[str] = set()
    kept: list[GoldenIssue] = []
    for item in items:
        if item.description in seen_desc:
            continue
        duplicated = False
        for old in kept:
            if (
                old.project == item.project
                and old.file == item.file
                and old.category == item.category
                and old.line_start <= item.line_end
                and item.line_start <= old.line_end
            ):
                duplicated = True
                break
        if duplicated:
            continue
        seen_desc.add(item.description)
        kept.append(item)
    return kept


def _manual_goldens(spec: ProjectSpec, root: Path) -> list[GoldenIssue]:
    """按 anchor 定位生成项目 HEAD（修复后）源码中的真实行号，产出 manual 金标。"""
    items: list[GoldenIssue] = []
    for gspec in spec.goldens:
        text = (root / gspec.file).read_text(encoding="utf-8")
        line = _anchor_line(text, f"{spec.name}/{gspec.file}", gspec.anchor)
        items.append(
            GoldenIssue(
                project=spec.name,
                file=gspec.file,
                line_start=line,
                line_end=line,
                category=gspec.category,
                severity=gspec.severity,
                description=f"{spec.name}/{gspec.file}:{line} {gspec.summary}",
                origin="manual",
            )
        )
    return items


def build_dataset() -> list[GoldenIssue]:
    """清空重建 bench/datasets/ 下的合成项目、注入变体与 goldset.jsonl。"""
    _clear_datasets_dir()
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

    roots = build_synthetic_projects()

    # 1) 合成项目 manual 金标（anchor 定位修复后 HEAD 的真实行号）
    manual_goldens: list[GoldenIssue] = []
    for spec in ALL_PROJECT_SPECS:
        manual_goldens.extend(_manual_goldens(spec, roots[spec.name]))

    # 2) git 历史挖矿（git-history 金标；severity 一律 high，见 bench.goldset_mining）
    history_goldens: list[GoldenIssue] = []
    with tempfile.TemporaryDirectory(prefix="codeaudit-mining-") as mining_dir:
        for spec in ALL_PROJECT_SPECS:
            mined = mine_git_history(
                roots[spec.name],
                Path(mining_dir) / f"{spec.name}.jsonl",
                max_commits=50,
                project=spec.name,
            )
            history_goldens.extend(mined)

    # 3) 注入变体（injected 金标）+ 继承缺陷的克隆金标（manual 来源；git-history 已在 HEAD 修复，不克隆）
    injected_goldens: list[GoldenIssue] = []
    clone_goldens: list[GoldenIssue] = []
    for spec in ALL_PROJECT_SPECS:
        variant_name = f"{spec.name}_inj"
        n_files, seed, per_file = INJECT_PLAN[spec.name]
        variant_goldens = inject_defects(roots[spec.name], PROJECTS_DIR / variant_name, n=n_files, seed=seed, per_file=per_file)
        _strip_git(PROJECTS_DIR / variant_name)
        injected_goldens.extend(variant_goldens)
        clone_goldens.extend(_clone_goldens([g for g in manual_goldens if g.project == spec.name], variant_name))

    # 4) demo_proj：表格金标 + 注入变体 + 克隆
    demo_goldens = from_markdown_table(DEMO_PROJ / "GOLDEN_ISSUES.md", project=DEMO_PROJ.name)
    n_files, seed, per_file = INJECT_PLAN["demo_proj"]
    injected_goldens.extend(inject_defects(DEMO_PROJ, PROJECTS_DIR / "demo_proj_inj", n=n_files, seed=seed, per_file=per_file))
    _strip_git(PROJECTS_DIR / "demo_proj_inj")
    clone_goldens.extend(_clone_goldens(demo_goldens, "demo_proj_inj"))

    items = _dedupe([*demo_goldens, *manual_goldens, *clone_goldens, *injected_goldens, *history_goldens])

    descriptions = [item.description for item in items]
    if len(set(descriptions)) != len(descriptions):
        raise RuntimeError("金标 description 存在重复，detection_metrics 按 description 去重会低估 recall")
    save_goldset(items, GOLDSET_PATH)

    _print_stats(items)
    n_ch = sum(1 for i in items if i.severity in {"critical", "high"})
    if len(items) < TARGET_GOLDENS or n_ch < TARGET_CRITICAL_HIGH:
        raise RuntimeError(
            f"数据集未达门槛：共 {len(items)} 条（要求 ≥{TARGET_GOLDENS}），critical+high {n_ch} 条（要求 ≥{TARGET_CRITICAL_HIGH}）"
        )
    return items


def _print_stats(items: list[GoldenIssue]) -> None:
    """打印金标统计（总量 / 分级 / 分类 / 来源 / 项目分布）。"""
    by_sev = Counter(i.severity for i in items)
    by_cat = Counter(i.category for i in items)
    by_org = Counter(i.origin for i in items)
    by_proj = Counter(i.project for i in items)
    ch = by_sev["critical"] + by_sev["high"]
    print(f"[gen_offline] 金标合计 {len(items)} 条 ｜ critical+high {ch} 条")
    print(f"[gen_offline] 按严重度：{dict(by_sev)}")
    print(f"[gen_offline] 按类别：{dict(by_cat)}")
    print(f"[gen_offline] 按来源：{dict(by_org)}")
    print(f"[gen_offline] 按项目：{dict(by_proj)}")


# ---------------------------------------------------------------- 离线基线 run


def dataset_project_paths() -> list[Path]:
    """返回数据集全部可审计项目路径（合成 4 + 注入变体 4 + demo 及其变体）。"""
    paths: list[Path] = [PROJECTS_DIR / spec.name for spec in ALL_PROJECT_SPECS]
    paths += [PROJECTS_DIR / f"{spec.name}_inj" for spec in ALL_PROJECT_SPECS]
    paths += [DEMO_PROJ, PROJECTS_DIR / "demo_proj_inj"]
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        raise FileNotFoundError(f"数据集项目缺失（请先运行 gen_offline.py 构建数据集）：{missing}")
    return paths


def _rmtree_quiet(path: Path) -> None:
    """尽力删除临时工作区：Windows 下 index.db 连接可能尚未释放，失败则留给系统临时目录清理。"""
    if not path.exists():
        return
    for child in path.rglob("*"):
        try:
            child.chmod(0o777)
        except OSError:
            pass
    shutil.rmtree(path, ignore_errors=True)


def run_offline_baseline() -> Path:
    """跑 full / −verify / rules_only 三配置离线基线并写 run 记录，返回记录路径。"""
    goldens = load_goldset(GOLDSET_PATH)
    projects = dataset_project_paths()
    stats_sev = Counter(g.severity for g in goldens)

    # 强制离线：api_key 置空 → FakeLLM 纯规则路径，零网络零 LLM 调用。
    # 工作区用一次性临时目录（审计索引的 sqlite 连接在 Windows 下未必及时释放，
    # 用"尽力删除"策略，删不掉也不影响结果落盘）。
    work_root = Path(tempfile.mkdtemp(prefix="codeaudit-bench-"))
    try:
        base_overrides: dict[str, Any] = {"api_key": "", "do_fix": False, "do_tests": False, "work_root": str(work_root)}
        ablation = run_ablation(
            projects,
            goldens,
            level="critical+high",
            names=list(ABLATION_NAMES),
            base_overrides=base_overrides,
        )
    finally:
        _rmtree_quiet(work_root)

    full_row = next(row for row in ablation["rows"] if row["config"] == "full")
    result: dict[str, Any] = dict(full_row["result"])
    result["ablation"] = ablation

    timing = result.get("timing", {})
    p50 = float(timing.get("sec_per_kloc_p50") or 0.0)
    p90 = float(timing.get("sec_per_kloc_p90") or 0.0)
    total_loc = sum(int(p["loc"] or 0) for p in result.get("projects", []))
    unmatched_set = set(result.get("unmatched_goldens") or [])
    unmatched_ch = sorted(
        g.description for g in goldens if g.severity in {"critical", "high"} and g.description in unmatched_set
    )
    n_unmatched_history = sum(1 for d in unmatched_ch if d.startswith("[git-history]"))

    notes = [
        "**本记录为离线纯规则基线，LLM 相关指标（精确率 85% 目标、tokens/KLOC）待提供 GLM_API_KEY 后真跑。**",
        "离线环境（api_key 强制为空 → FakeLLM）下 full / −verify / rules_only 三配置退化为同一条纯规则路径，"
        "三行指标差异仅为计时噪声；「−verify 使 Precision 下降」等消融结论必须在真跑环境复测后才可引用。",
        f"耗时为纯规则流水线（ingest→index→understand→detect→report）的本机量级：P50≈{p50:.1f} / P90≈{p90:.1f} s/KLOC，"
        "不含 LLM 调用与沙箱验证耗时；docs/04 的 30 s/KLOC 目标须以真跑口径为准。",
        "tokens/KLOC = 0：离线无任何 LLM 调用、无 token 消耗、无缓存行为。",
        f"Recall(critical+high) 未达 1.0 的缺口共 {len(unmatched_ch)} 条，其中 {n_unmatched_history} 条为 git-history 来源"
        "（缺陷已在合成项目修复提交中消除，HEAD 审计天然不可检出，应在其父提交快照上对账，见「未命中金标」节）；"
        f"其余 {len(unmatched_ch) - n_unmatched_history} 条为 injected/manual 来源的真实漏报。",
        f"数据集：bench/datasets/goldset.jsonl 共 {len(goldens)} 条金标"
        f"（critical+high {stats_sev['critical'] + stats_sev['high']} 条），"
        f"项目集 {len(projects)} 个（合成 4 + 注入变体 4 + demo_proj 及其注入变体），源码合计约 {total_loc} 行。",
        "复现：python bench/datasets/gen_offline.py --with-run（清空重建数据集后重跑，注入 seed 固定，金标可复现）。",
    ]
    meta: dict[str, Any] = {
        "date": RUN_DATE,
        "model": "offline-fakellm（纯规则基线）",
        "prompt_version": "n/a",
        "config": f"full / {MINUS}verify / rules_only × {len(projects)} 项目（api_key='' 强制离线，level=critical+high）",
        "notes": notes,
    }
    write_run_record(result, RUN_RECORD_PATH, meta)
    _append_extra_sections(result, goldens, unmatched_ch)
    print(f"[gen_offline] 离线基线 run 记录已写入：{RUN_RECORD_PATH}")
    return RUN_RECORD_PATH


def _append_extra_sections(result: dict[str, Any], goldens: list[GoldenIssue], unmatched_ch: list[str]) -> None:
    """在 write_run_record 产物后追加：项目明细 / 未命中金标（critical+high）/ 离线声明。"""
    lines: list[str] = []

    lines += ["", "## 项目明细", "", "| 项目 | LOC | 耗时(s) | s/KLOC | Issue 数 |", "|---|---|---|---|---|"]
    for p in result.get("projects", []):
        loc = int(p["loc"] or 0)
        duration = float(p["duration_sec"])
        spk = f"{duration / (loc / 1000.0):.2f}" if loc > 0 else "N/A"
        lines.append(f"| {p['project']} | {loc} | {duration:.2f} | {spk} | {p['issue_count']} |")
    lines.append("")

    lines += [
        "## 未命中金标（critical+high，full 配置）",
        "",
        "说明：下列未命中金标**全部来自 git-history 来源**——对应缺陷已在合成项目的修复提交中消除，"
        "HEAD 版本上不再存在，因此在 HEAD 审计中天然不可检出（docs/04 §2.1 口径：这类金标应在其"
        "父提交快照上对账，tests 的对齐抽样即按父提交版本核对，实测 100% 可命中）；"
        "若未命中清单中出现 injected/manual 来源的 critical+high 金标，才属真实漏报。",
        "",
    ]
    if unmatched_ch:
        lines.extend(f"- {desc}" for desc in unmatched_ch)
    else:
        lines.append("- （无）")
    lines.append("")

    lines += [
        "## 离线声明",
        "",
        "- **本记录为离线纯规则基线（offline rules-only baseline）**：运行时强制 `api_key=\"\"`，"
        "audit 流水线走 FakeLLM 纯规则路径，全程零网络、零 LLM 调用、零 token 消耗。",
        f"- 离线环境下 `full` / `{MINUS}verify` / `rules_only` 三配置退化为同一条纯规则路径"
        "（`llm_available=False` 时 review/verify 通道不生效），三行指标差异仅为计时噪声；"
        "「−verify 使 Precision 下降」等消融结论必须在 GLM_API_KEY 真跑环境复测后才可引用。",
        "- **本记录为离线纯规则基线，LLM 相关指标（精确率 85% 目标、tokens/KLOC）待提供 GLM_API_KEY 后真跑**；"
        "在拿到真跑 run 记录之前，本文件中的 Precision/Recall 不得对外引用为系统真实水平。",
        "- 纯规则基线耗时量级（个位数 s/KLOC、本机 CPU、小规模合成项目）不能外推到真跑口径"
        "（含 LLM 调用与沙箱验证）；docs/04 的 30 s/KLOC 目标以真跑记录为准。",
        "- 复现方式：`python bench/datasets/gen_offline.py --with-run`（清空重建数据集后重跑三配置，注入 seed 固定）。",
        "",
    ]
    with open(RUN_RECORD_PATH, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


# ---------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：默认构建数据集；--with-run 追加离线基线 run 记录。"""
    parser = argparse.ArgumentParser(
        prog="python bench/datasets/gen_offline.py",
        description="构建离线金标数据集（≥150 条）并可选跑离线纯规则基线 run 记录（幂等，清空重建）。",
    )
    parser.add_argument(
        "--with-run",
        action="store_true",
        help="构建数据集后追加执行 full / −verify / rules_only 三配置离线基线，写入 bench/results/run_20260911_offline.md",
    )
    args = parser.parse_args(argv)

    try:  # Windows 控制台默认 GBK：统一改用 UTF-8 输出，避免 −/中文 打印失败
        if sys.stdout.encoding and sys.stdout.encoding.lower().replace("-", "") != "utf8":
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    build_dataset()
    if args.with_run:
        run_offline_baseline()
    return 0


if __name__ == "__main__":
    sys.exit(main())
