"""金丝雀双版本回放子包（W10-A3）：L2 灰度基建。

入口：

- ``python -m bench.canary.replay [--old-tag v0.5.0] [--new-ref HEAD]
  [--port-base 8915] [--out md路径] [--quick]``
  （git worktree 检出旧 tag 起双端口服务，固定请求集回放 + 归一化 diff，
  见 :mod:`bench.canary.replay`）

约定：全程离线（启动前清除 GLM_API_KEY / GLM_BASE_URL / GLM_MODEL，子进程继承
净化后的环境）；写操作只落 bench/canary/**、--out 指定路径与系统临时目录；
不修改 audit/** 与 server/**（旧版代码经 git worktree 只读检出，依赖共用当前环境）。
"""

from __future__ import annotations

__all__ = ["replay"]
