"""应用配置：故意包含硬编码密钥等问题。"""

API_KEY = "sk-live-9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c"

DEBUG = True

DB_URL = "mysql://root:P@ssw0rd@localhost/app"

def _inj_hardcoded_secret_1():
    access_token = "sk-live-9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c"
    return access_token

def _inj_str_concat_loop_2(rows):
    html = ""
    for row in rows:
        html = html + "<li>" + str(row) + "</li>"
    return html

def _inj_urlopen_no_timeout_3(url):
    import urllib.request

    with urllib.request.urlopen(url) as resp:
        return resp.read()

def _inj_list_membership_4(items):
    seen = []
    for item in items:
        if item in seen:
            continue
        seen.append(item)
    return seen
