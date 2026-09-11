"""网络工具：无超时的请求。"""

import urllib.request


def fetch_json(url):
    with urllib.request.urlopen(url) as resp:  # line 7: urlopen 未设置 timeout
        return resp.read()


def fetch_json_with_timeout(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read()

def _inj_list_membership_1(items):
    seen = []
    for item in items:
        if item in seen:
            continue
        seen.append(item)
    return seen

def _inj_str_concat_loop_2(rows):
    html = ""
    for row in rows:
        html = html + "<li>" + str(row) + "</li>"
    return html

def _inj_eq_none_3(value):
    if value == None:
        return False
    return True

def _inj_sql_concat_4(conn, user_id):
    query = "SELECT * FROM users WHERE id = " + user_id
    return conn.execute(query)
