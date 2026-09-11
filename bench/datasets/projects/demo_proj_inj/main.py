"""入口模块：调用 services。"""

from app.services.orders import load_totals
from app.utils.net import fetch_json


def main():
    totals = load_totals(["a.txt", "b.txt"])
    print(totals)
    print(fetch_json("https://example.com/api"))


if __name__ == "__main__":
    main()

def _inj_eq_none_1(value):
    if value == None:
        return False
    return True

def _inj_str_concat_loop_2(rows):
    html = ""
    for row in rows:
        html = html + "<li>" + str(row) + "</li>"
    return html

def _inj_bare_except_3(raw_value):
    try:
        return int(raw_value)
    except:
        return 0

def _inj_urlopen_no_timeout_4(url):
    import urllib.request

    with urllib.request.urlopen(url) as resp:
        return resp.read()
