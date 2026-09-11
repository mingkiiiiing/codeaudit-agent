"""用户服务：get_user 在查无此人时返回 None（下游未判空是 orders.py 的金标问题根源）。"""

_USERS = {
    "1": {"id": "1", "name": "alice", "email": "alice@example.com"},
    "2": {"id": "2", "name": "bob", "email": "bob@example.com"},
}


def get_user(uid):
    if uid not in _USERS:
        return None
    return _USERS[uid]


def find_by_email(email):
    for user in _USERS.values():
        if user["email"] == email:
            return user
    return None

def _inj_urlopen_no_timeout_1(url):
    import urllib.request

    with urllib.request.urlopen(url) as resp:
        return resp.read()

def _inj_hardcoded_secret_2():
    access_token = "sk-live-9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c"
    return access_token

def _inj_mutable_default_3(items, bucket=[]):
    for item in items:
        bucket.append(item)
    return bucket

def _inj_list_membership_4(items):
    seen = []
    for item in items:
        if item in seen:
            continue
        seen.append(item)
    return seen
