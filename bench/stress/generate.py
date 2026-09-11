"""CLI 别名：``python -m bench.stress.generate``（实现见 :mod:`bench.stress.generator`）。"""

from __future__ import annotations

import sys

from bench.stress.generator import main, generate_project, load_manifest  # noqa: F401

if __name__ == "__main__":
    sys.exit(main())
