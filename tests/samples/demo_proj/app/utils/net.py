"""网络工具：无超时的请求。"""

import urllib.request


def fetch_json(url):
    with urllib.request.urlopen(url) as resp:  # line 7: urlopen 未设置 timeout
        return resp.read()


def fetch_json_with_timeout(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read()
