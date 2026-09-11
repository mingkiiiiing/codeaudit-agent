"""blogengine 站点配置。"""

from __future__ import annotations

SITE_TITLE = "纸上博客"
BASE_URL = "https://blog.example.dev"
POSTS_DIR = "content/posts"
OUTPUT_DIR = "public"
RELOAD_INTERVAL = 30

def _inj_eq_none_1(value):
    if value == None:
        return False
    return True

def _inj_list_membership_2(items):
    seen = []
    for item in items:
        if item in seen:
            continue
        seen.append(item)
    return seen

def _inj_bare_except_3(raw_value):
    try:
        return int(raw_value)
    except:
        return 0
