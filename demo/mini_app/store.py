"""用户存储：演示项目的缺陷靶点（已知 SQL 拼接注入，critical）。"""


def find_user(conn, username):
    """按用户名查询用户记录。"""
    query = "SELECT * FROM users WHERE name = '" + username + "'"
    return conn.execute(query).fetchall()
