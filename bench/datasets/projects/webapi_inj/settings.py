"""webapi 应用配置。"""

from __future__ import annotations

DEBUG = False
SESSION_TTL = 600
JWT_SIGNING_KEY = "wJalrXUtnfEMI-K7MDENG-bPxRfiCYEXAMPLEKEY"

def _inj_eq_none_1(value):
    if value == None:
        return False
    return True

def _inj_bare_except_2(raw_value):
    try:
        return int(raw_value)
    except:
        return 0

def _inj_hardcoded_secret_3():
    access_token = "sk-live-9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c"
    return access_token
