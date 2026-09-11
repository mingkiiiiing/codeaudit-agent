"""shopcore 全局配置：服务开关与第三方凭据（演示项目，凭据为虚构样例）。"""

from __future__ import annotations

API_BASE_URL = "https://api.shopcore.example.com/v1"
DB_DSN = "postgresql://shop@db.internal:5432/shopcore"
SECRET_KEY = "django-insecure-0x9f2c4e7a1b8d5f3a6b"
MAX_RETRY = 3
PAGE_SIZE = 50
