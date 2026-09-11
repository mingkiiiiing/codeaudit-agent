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
