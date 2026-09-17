"""W19-B 并发缺陷规则正反用例（PY-UNSYNCED-SHARED-MUTATION / PY-SLEEP-IN-ASYNC）。

直接构造 RuleContext（conftest 的 make_ctx），不依赖 registry 接线——接线由
集成人统一完成，本文件只验证规则自身行为。
"""

from __future__ import annotations

from audit.detect.rules.py_concurrency import (
    SleepInAsyncRule,
    UnsyncedSharedMutationRule,
)


def _lines(rule, ctx):
    return [h.line_start for h in rule.check(ctx)]


class TestUnsyncedSharedMutation:
    rule = UnsyncedSharedMutationRule()

    # ------------------------------------------------------------ 正例

    def test_positive_augmented_counter(self, make_ctx):
        ctx = make_ctx(
            "import threading\n"
            "\n"
            "counter = 0\n"
            "\n"
            "def worker():\n"
            "    global counter\n"
            "    counter += 1\n"
        )
        assert _lines(self.rule, ctx) == [7]

    def test_positive_list_append(self, make_ctx):
        # 容器变异不需要 global 声明，规则同样不能要求
        ctx = make_ctx(
            "import threading\n"
            "\n"
            "results = []\n"
            "\n"
            "def collect(item):\n"
            "    results.append(item)\n"
        )
        assert _lines(self.rule, ctx) == [6]

    def test_positive_multiprocessing_dict_update(self, make_ctx):
        ctx = make_ctx(
            "import multiprocessing\n"
            "\n"
            "cache = {}\n"
            "\n"
            "def store(k, v):\n"
            "    cache.update({k: v})\n"
        )
        assert _lines(self.rule, ctx) == [6]

    def test_positive_annotated_counter(self, make_ctx):
        # 带类型注解的模块级初始化同样算模块级可变变量
        ctx = make_ctx(
            "import threading\n"
            "counter: int = 0\n"
            "\n"
            "def bump():\n"
            "    global counter\n"
            "    counter -= 1\n"
        )
        assert _lines(self.rule, ctx) == [6]

    def test_positive_meta_carries_variable_and_func(self, make_ctx):
        ctx = make_ctx(
            "import threading\n"
            "hits = []\n"
            "\n"
            "def record(x):\n"
            "    hits.append(x)\n"
        )
        hit = self.rule.check(ctx)[0]
        assert hit.meta["variable"] == "hits"
        assert hit.meta["func"] == "record"

    # ------------------------------------------------------------ 反例

    def test_negative_no_threading_import(self, make_ctx):
        # 条件①缺失：无 threading/multiprocessing 导入，整文件豁免
        ctx = make_ctx(
            "import json\n"
            "\n"
            "counter = 0\n"
            "\n"
            "def worker():\n"
            "    global counter\n"
            "    counter += 1\n"
        )
        assert self.rule.check(ctx) == []

    def test_negative_locked_with_block(self, make_ctx):
        ctx = make_ctx(
            "import threading\n"
            "\n"
            "counter = 0\n"
            "lock = threading.Lock()\n"
            "\n"
            "def worker():\n"
            "    global counter\n"
            "    with lock:\n"
            "        counter += 1\n"
        )
        assert self.rule.check(ctx) == []

    def test_negative_lock_acquire_style(self, make_ctx):
        # 手动加解锁风格：函数内出现 .acquire( 即整函数豁免
        ctx = make_ctx(
            "import threading\n"
            "\n"
            "counter = 0\n"
            "lock = threading.Lock()\n"
            "\n"
            "def worker():\n"
            "    global counter\n"
            "    lock.acquire()\n"
            "    try:\n"
            "        counter += 1\n"
            "    finally:\n"
            "        lock.release()\n"
        )
        assert self.rule.check(ctx) == []

    def test_negative_local_variable_shadow(self, make_ctx):
        # 条件②缺失：total 是函数局部变量，不是模块级可变状态
        ctx = make_ctx(
            "import threading\n"
            "\n"
            "def worker():\n"
            "    total = 0\n"
            "    total += 1\n"
        )
        assert self.rule.check(ctx) == []

    def test_negative_read_only(self, make_ctx):
        # 只读不变异：模块级变量被读取不算读改写
        ctx = make_ctx(
            "import threading\n"
            "\n"
            "counter = 0\n"
            "\n"
            "def reader():\n"
            "    return counter\n"
        )
        assert self.rule.check(ctx) == []

    def test_negative_mutation_outside_function(self, make_ctx):
        # 条件③限定"函数体内"：模块顶层的同形语句不归本规则（导入时执行一次）
        ctx = make_ctx(
            "import threading\n"
            "\n"
            "results = []\n"
            "results.append(1)\n"
        )
        assert self.rule.check(ctx) == []

    def test_negative_in_string_and_comment(self, make_ctx):
        # 字符串字面量与注释里的同形文本不报（掩码扫描）
        ctx = make_ctx(
            "import threading\n"
            "\n"
            "counter = 0\n"
            "\n"
            "def worker():\n"
            "    note = 'counter += 1'  # counter += 1\n"
        )
        assert self.rule.check(ctx) == []

    def test_negative_instance_attr_not_module_state(self, make_ctx):
        # self.count 前有点号，不匹配模块级变量名的裸引用
        ctx = make_ctx(
            "import threading\n"
            "\n"
            "class Job:\n"
            "    def __init__(self):\n"
            "        self.count = 0\n"
            "\n"
            "    def run(self):\n"
            "        self.count += 1\n"
        )
        assert self.rule.check(ctx) == []


class TestSleepInAsync:
    rule = SleepInAsyncRule()

    def test_positive_async_time_sleep(self, make_ctx):
        ctx = make_ctx(
            "import time\n"
            "\n"
            "async def poll():\n"
            "    time.sleep(1)\n"
        )
        assert _lines(self.rule, ctx) == [4]

    def test_positive_time_import_alias(self, make_ctx):
        ctx = make_ctx(
            "import time as t\n"
            "\n"
            "async def poll():\n"
            "    t.sleep(1)\n"
        )
        assert _lines(self.rule, ctx) == [4]

    def test_positive_from_time_import_sleep(self, make_ctx):
        ctx = make_ctx(
            "from time import sleep\n"
            "\n"
            "async def poll():\n"
            "    sleep(1)\n"
        )
        assert _lines(self.rule, ctx) == [4]

    def test_positive_sleep_inside_async_loop(self, make_ctx):
        # async def 体内多层嵌套（while/try）仍属 async 范围
        ctx = make_ctx(
            "import time\n"
            "\n"
            "async def poll():\n"
            "    while True:\n"
            "        try:\n"
            "            time.sleep(0.5)\n"
            "        except OSError:\n"
            "            break\n"
        )
        assert _lines(self.rule, ctx) == [6]

    def test_negative_sync_def_sleep(self, make_ctx):
        # 对照：同步函数里的 time.sleep 不阻塞事件循环，不报
        ctx = make_ctx(
            "import time\n"
            "\n"
            "def poll():\n"
            "    time.sleep(1)\n"
        )
        assert self.rule.check(ctx) == []

    def test_negative_asyncio_sleep(self, make_ctx):
        # 对照：asyncio.sleep 是正确写法
        ctx = make_ctx(
            "import asyncio\n"
            "\n"
            "async def poll():\n"
            "    await asyncio.sleep(1)\n"
        )
        assert self.rule.check(ctx) == []

    def test_negative_sleep_in_string(self, make_ctx):
        ctx = make_ctx(
            "import time\n"
            "\n"
            "async def poll():\n"
            "    doc = 'time.sleep(1)'\n"
        )
        assert self.rule.check(ctx) == []

    def test_negative_async_without_sleep(self, make_ctx):
        ctx = make_ctx(
            "import time\n"
            "\n"
            "async def poll():\n"
            "    await tick()\n"
        )
        assert self.rule.check(ctx) == []


if __name__ == "__main__":  # pragma: no cover
    import pytest

    pytest.main([__file__])
