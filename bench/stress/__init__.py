"""压力测试子包（W5-A2）：合成项目生成器与压测运行器。

入口：

- ``python -m bench.stress.generate --files 2000 --out bench/stress/data/proj2k``
  （生成器，见 :mod:`bench.stress.generator`；``bench.stress.generate`` 为同名 CLI 别名）
- ``python -m bench.stress.run_stress [--scale 500|2000|all] [--out md路径]``
  （六场景压测，产出 markdown 报告）

约定：全部场景零网络；压测数据默认放 ``bench/stress/data/``（已加入 .gitignore，
不入库）；所有写操作只落在 bench/stress/** 与临时目录，不修改 audit/**。
"""

from __future__ import annotations

__all__ = ["generator", "run_stress"]
