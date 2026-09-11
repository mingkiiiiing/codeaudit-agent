"""shopcore 全局配置：服务开关与第三方凭据（演示项目，凭据为虚构样例）。"""

from __future__ import annotations

API_BASE_URL = "https://api.shopcore.example.com/v1"
DB_DSN = "postgresql://shop@db.internal:5432/shopcore"
SECRET_KEY = "django-insecure-0x9f2c4e7a1b8d5f3a6b"
MAX_RETRY = 3
PAGE_SIZE = 50

def _inj_str_concat_loop_1(rows):
    html = ""
    for row in rows:
        html = html + "<li>" + str(row) + "</li>"
    return html

def _inj_urlopen_no_timeout_2(url):
    import urllib.request

    with urllib.request.urlopen(url) as resp:
        return resp.read()

def _inj_hardcoded_secret_3():
    access_token = "sk-live-9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c"
    return access_token
