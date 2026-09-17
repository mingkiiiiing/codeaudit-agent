"""audit.depcheck：依赖清单安全扫描 + 配置明文密钥扫描（W16 验收短板清偿）。

模块组成：
- manifest：requirements*.txt / package.json / pyproject.toml → Dependency；
- advisory：种子漏洞库（db/advisory_db.json）与版本区间交集匹配（DEP-CVE）；
- osv：OSV 在线漏洞查询（opt-in，CODEAUDIT_OSV_ONLINE=1 才触网，默认离线）；
- configsecret：配置文件明文密钥扫描（CFG-SECRET）；
- scanner：run(ctx) 挂点入口（audit.detect.engine._post_scan_issues 延迟调用），
  含冗余依赖检出（DEP-UNUSED）与 OSV 在线结果合并。

对外契约见 scanner 模块 docstring；默认不依赖网络（漏洞数据离线随包分发，
OSV 在线为显式 opt-in 增强）。
"""

from __future__ import annotations
