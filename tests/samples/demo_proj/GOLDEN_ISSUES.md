# demo_proj 金标缺陷清单（供 T4 规则测试与 T6 bench 冒烟）

| # | 文件 | 行 | 类别 | 严重度 | 描述 |
|---|---|---|---|---|---|
| G1 | app/services/orders.py | 10 | bug | high | get_user 可能返回 None，未判空即取下标 |
| G2 | app/services/orders.py | 11 | security | critical | SQL 语句字符串拼接，存在注入 |
| G3 | app/services/orders.py | 20 | performance | medium | list 做 in 成员判断，应使用 set |
| G4 | app/services/orders.py | 23 | bug | high | open() 打开文件未关闭（资源泄漏） |
| G5 | app/services/orders.py | 33 | bug | medium | 裸 except: pass 吞掉全部异常 |
| G6 | app/utils/mathx.py | 4 | bug | high | 可变默认参数 bucket=[] |
| G7 | app/utils/mathx.py | 11 | bug | low | == None 应为 is None |
| G8 | app/utils/mathx.py | 18-19 | performance | low | 循环内字符串 + 拼接，应使用 join |
| G9 | app/utils/mathx.py | 24-25 | style | low | 魔法数字 10000/0.85 |
| G10 | app/config.py | 3 | security | critical | 硬编码 API 密钥 |
| G11 | app/utils/net.py | 7 | performance | medium | urlopen 未设置 timeout |
| G12 | app/services/orders.py | 11 | bug | critical | SQL 拼接（bug 视角：非法输入直接崩溃） |
