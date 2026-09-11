"""JS/TS 静态规则库包：JavaScript 与 TypeScript 规则及共享扫描器。"""

from __future__ import annotations

from audit.detect.rules.js.javascript import build_javascript_rules
from audit.detect.rules.js.typescript import build_typescript_rules

__all__ = ["build_javascript_rules", "build_typescript_rules"]
