# 规则手册

> 本页由 `scripts/gen_rule_docs.py` 从规则注册表（audit/detect/registry.py 的 DEFAULT_REGISTRY）自动生成——请勿手改；新增或调整规则后重新运行即可。

当前共 **86** 条内置规则；JS/TS 共享规则（security 类）同时作用于两种语言。

## 规则总表

### Python（52 条）

#### bug 缺陷（17 条）

| 规则 ID | 语言 | 类别 | 严重度 | 说明 |
| --- | --- | --- | --- | --- |
| PY-ASSERT-IN-PROD | Python | bug | medium | 生产代码使用 assert 做输入校验：python -O 运行时 assert 会被整体剥离，校验静默失效。 |
| PY-ASSERT-TUPLE | Python | bug | high | if/while 的条件是带括号的元组（如 `if (a, b):`）：非空元组恒为真，分支永远执行，作者多半想写 `if a and b:` 或 `if a == (x, y):`，属必现逻辑错误。 |
| PY-BARE-EXCEPT | Python | bug | medium | 使用裸 `except:` 会捕获包括 SystemExit/KeyboardInterrupt 在内的一切异常，且丢失异常上下文。 |
| PY-EMPTY-EXCEPT | Python | bug | low | except 分支体只有 `...` 或字符串占位（如 docstring），异常处理逻辑尚未实现或被遗忘。 |
| PY-EQ-NONE | Python | bug | low | 用 `==`/`!=` 与 None 比较：重载了 __eq__ 的对象会得到错误结果，应使用 `is`/`is not`。 |
| PY-EXCEPT-PASS | Python | bug | medium | except 分支体只有 pass：异常被静默吞掉，出错时既无日志也无恢复动作。 |
| PY-IS-LITERAL | Python | bug | low | 用 `is` 与字符串/数字等字面量比较：is 是身份比较，字面量身份无保证，结果随解释器实现而异。 |
| PY-MUTABLE-CLASS-ATTR | Python | bug | medium | 类体中以 []/{}/set() 等可变字面量定义类属性：该对象挂在类上、全部实例共享，任一实例原地修改会波及其他实例；应在 __init__ 中为每个实例创建新对象。 |
| PY-MUTABLE-DEFAULT | Python | bug | high | 函数默认参数使用 []/{}/set() 等可变对象：默认值在多次调用间共享，跨调用累积脏数据。 |
| PY-NONE-DEREF | Python | bug | medium | 变量在同函数内被赋值为 None 后未经重赋值即被解引用（属性/下标/调用）：运行时必然抛出 AttributeError/TypeError；请先判空（if x is not None）、给变量赋有效值，或重构掉 None 占位状态。 |
| PY-OPEN-NO-CLOSE | Python | bug | high | open() 的返回值既未用 with 管理，也未在作用域内显式 close：文件句柄泄漏。 |
| PY-OPEN-WITHOUT-ENCODING | Python | bug | low | open() 打开文本文件未显式传 encoding=：实际编码随平台与 locale 变化（Windows 常为 GBK/cp936，Linux 为 UTF-8），同一份代码跨平台读写中文即乱码或抛 DecodeError。 |
| PY-RETURN-IN-INIT | Python | bug | high | __init__ 中 `return` 了非 None 值：Python 规定构造器必须返回 None，实例化时直接抛 `TypeError: __init__() should return None`，属必现运行时错误。 |
| PY-SHADOW-BUILTIN | Python | bug | low | 把 list/dict/str 等内置名用作变量名：遮蔽内置函数，后续同作用域调用原始内置将直接报错。 |
| PY-SUBPROCESS-WITHOUT-CHECK | Python | bug | high | subprocess.run/call 未设置 check=True：命令非零退出不抛异常，失败被静默吞掉，后续步骤在错误前提下继续执行；应显式传 check=True 或自行检查 returncode。 |
| PY-UNREACHABLE-CODE | Python | bug | medium | 控制流终止语句（return/raise/break/continue）之后存在同缩进语句：永远不可达，多为逻辑错误或死代码。 |
| PY-UNSYNCED-SHARED-MUTATION | Python | bug | medium | 文件导入 threading/multiprocessing 且存在模块级可变变量（[]/{}/set()/0），函数内对其做增强赋值或 append/add/update 变异却无锁保护：多线程并发时读-改-写交错会丢失更新；应用 Lock 保护临界区、改用 queue.Queue 或原子操作。疑似级启发：静态无法证明多线程实际调用。 |

#### performance 性能（9 条）

| 规则 ID | 语言 | 类别 | 严重度 | 说明 |
| --- | --- | --- | --- | --- |
| PY-DEEPCOPY-IN-LOOP | Python | performance | medium | 循环内调用 copy.deepcopy：深拷贝是极重的递归操作，循环内反复执行会显著拖慢程序。 |
| PY-IO-IN-LOOP | Python | performance | high | 循环体内执行文件/数据库/网络 IO：每次迭代都付出完整 IO 往返成本，应移出循环或批量处理。 |
| PY-LIST-MEMBERSHIP | Python | performance | medium | 用 list 做 `in` 成员判断是 O(n) 线性扫描，数据量大时应改用 set/dict（O(1)）。 |
| PY-NO-TIMEOUT | Python | performance | medium | 网络请求未设置 timeout：默认无限等待，远端无响应时调用方将永久挂起。 |
| PY-ORM-N-PLUS-ONE | Python | performance | medium | for/while 循环体内逐条执行 ORM 查询（SQLAlchemy `session.query(X).get(/.first(/.all(` 同行链式，或 Django `Model.objects.get(`）：循环 N 次即发送 N 条 SQL（N+1 问题），数据量增大时延迟线性放大；应改为一次性批量查询或关系预加载。 |
| PY-REPEAT-CALL | Python | performance | low | 同一函数内重复执行参数完全相同的函数调用：若调用非幂等或有开销，应提取为局部变量复用。 |
| PY-SLEEP-IN-ASYNC | Python | performance | medium | async def 体内调用同步 time.sleep：阻塞整个事件循环，期间所有协程（含其他请求）都无法调度，异步吞吐骤降；应改用 `await asyncio.sleep(...)`。 |
| PY-STR-CONCAT-LOOP | Python | performance | low | 循环内对字符串做 +/+= 拼接：每次都生成新字符串对象，总体 O(n²)；应收集到列表后 ''.join()。 |
| PY-STRING-FORMAT-IN-LOGGING | Python | performance | low | 在 logging 调用里用 %/.format/f-string 预先拼好消息：即使该级别被过滤也会付出格式化成本，且丢失日志聚合字段；应改用惰性参数化 `logger.info('value: %s', x)`。 |

#### style 风格（12 条）

| 规则 ID | 语言 | 类别 | 严重度 | 说明 |
| --- | --- | --- | --- | --- |
| PY-CYCLOMATIC-COMPLEXITY | Python | style | medium | 函数圈复杂度超过阈值：决策分支过多意味着路径组合爆炸，难以测试与维护；应拆分为更小函数，或用卫语句/字典分派降低分支。 |
| PY-DEEP-NESTING | Python | style | medium | 代码嵌套达到 4 层以上：认知负担大、分支组合爆炸，应通过提前返回/抽取函数降低深度。 |
| PY-GLOBAL-STATE-MUTATE | Python | style | low | 函数内通过 `global` 声明并对模块级变量赋值：写操作散落在函数间、执行顺序隐式耦合，测试互相污染且并发不安全；应改为显式传参/返回值，或收敛到类/单例中管理。 |
| PY-HARDCODED-URL | Python | style | low | 在非常量位置硬编码 URL：环境切换（测试/预发/生产）时需改代码，应提取为配置或常量。 |
| PY-LAYER-VIOLATION | Python | style | medium | 高层目录（api/controller 等）直接依赖低层目录（dao/repository/db 等），跳过 service 中间层：表现层与存储实现强耦合，替换存储或复用接口困难。若项目无中间层约定可忽略或调整目录命名；建议依赖经服务层转发，或以依赖倒置（接口/协议）解耦。 |
| PY-LONG-FUNCTION | Python | style | medium | 函数体超过 80 行：职责过多、难以测试与复用，应拆分为更小的函数。 |
| PY-MAGIC-NUMBER | Python | style | low | 代码中出现无命名的大数字面量（≥1000）：含义不明、散落多处难以统一修改，应提取为具名常量。 |
| PY-NAMING-STYLE | Python | style | low | 命名不符合 PEP 8：函数/方法名应全小写下划线分词（snake_case），类名应以大写字母开头的驼峰式（PascalCase）；命名不一致会降低可读性并让「大小写敏感的外部调用」埋下隐蔽缺陷。 |
| PY-PINYIN-NAMING | Python | style | low | 标识符疑似拼音命名（如 jieguo/jisuan/shuju）：中英混用的拼音命名会显著降低代码可读性与可检索性，团队协作时难以理解意图；建议统一改用英文命名。本条为保守启发式，仅对可完整拆分为 ≥2 个常见拼音音节且非英文词的标识符提示。 |
| PY-PRINT-DEBUG | Python | style | low | 生产代码中使用 print 输出：绕过日志体系，无级别/时间/上下文，且影响性能与日志采集。 |
| PY-TODO-FIXME | Python | style | low | 注释中的 TODO/FIXME/HACK 标记：代表已知未完成事项或临时绕过，应在交付前清理或建跟踪项。 |
| PY-TYPE-COMPARE | Python | style | low | 用 `type(x) ==/is ...` 做类型判断：绕开继承体系（子类实例判为不等），且绕过 `__eq__` 语义；应改用 `isinstance(x, T)`。 |

#### security 安全（14 条）

| 规则 ID | 语言 | 类别 | 严重度 | 说明 |
| --- | --- | --- | --- | --- |
| PY-COMMAND-INJECTION | Python | security | high | 通过 os.system/os.popen 执行拼接了变量的命令：输入含 shell 元字符时可被命令注入。亦覆盖 subprocess.Popen 动态首参或 shell=True（字符串参数会进入 shell）、os.execv* 以 sh/bash -c 调 shell，以及 run/call/check_output/Popen 列表参数经 `sh -c <动态命令>` 中转的等价注入形态。 |
| PY-DEFAULT-CREDENTIAL | Python | security | high | 凭据形态变量（password/token/api_key 等）被赋以知名出厂默认/弱口令（admin/123456/root 等，内置字典整词比对）：攻击者可用公开默认凭据直接登录；与 PY-HARDCODED-SECRET（高熵密钥）互补，专抓低熵默认口令。 |
| PY-DYNAMIC-COMPILE | Python | security | medium | 调用内建 compile() 且源码首参为变量/拼接（非静态字面量）：动态编译源码等价任意代码执行入口，配合 eval/exec 即可执行不可信代码。 |
| PY-DYNAMIC-IMPORT | Python | security | medium | 以变量/拼接动态导入模块（__import__/importlib.import_module 首参非静态字面量）：用户可控的动态导入可被加载任意模块（恶意路径/覆盖标准库），建议白名单映射。 |
| PY-EVAL-EXEC | Python | security | critical | 使用 eval/exec 动态执行代码：输入可被控制时等价于任意代码执行漏洞。 |
| PY-HARDCODED-SECRET | Python | security | critical | 密钥/口令硬编码在源码中：随代码库扩散，任何有读权限的人都能获取凭据。 |
| PY-INDIRECT-EXEC | Python | security | low | getattr 参数字符串字面量含危险内建名（eval/exec/__import__ 等），或 globals()/locals() 下标取值后立即调用：间接动态执行（标疑）——静态无法确证可控性，请人工确认。 |
| PY-LOG-FORGERY | Python | security | low | logger/print 日志调用的格式字符串含换行转义或多行字面量，且同一调用拼接了非常量变量：外部输入可注入伪造日志行（log forging / CVE 型日志注入）；建议对用户输入做换行/控制字符清洗或改用结构化日志。 |
| PY-PII-LOG | Python | security | medium | 疑似将个人敏感信息（手机号/身份证号等）写入日志且未脱敏：依据个人信息保护相关要求（如《个人信息保护法》的最小必要原则），日志中的个人信息应脱敏或最小化，明文落盘会随日志采集/归档扩散泄露面，且难以事后回收。 |
| PY-PII-SQL | Python | security | medium | 疑似将个人敏感信息（手机号/身份证号/邮箱等）以明文写入数据库：SQL 写库语句（INSERT INTO/UPDATE/CREATE TABLE）中出现 PII 列名，或执行 SQL 时绑定了 PII 变量。依据个人信息保护相关要求（如《个人信息保护法》的最小必要原则），敏感个人信息的存储应加密/脱敏/最小化，明文入库会随数据库、备份与导出链路长期留存并扩大泄露面。注意：本规则关注数据最小化而非 SQL 注入——参数化绑定（占位符）本身能防注入，但 PII 变量明文绑定入库仍会命中；非敏感字段的参数化查询不报。建议：对敏感字段做字段级加密、单向哈希（需检索时用 HMAC/盲索引），或仅收集与存储业务必需的最小字段。 |
| PY-SQL-INJECTION | Python | security | critical | SQL 语句以字符串拼接方式引入外部输入：攻击者可注入任意 SQL，导致数据泄露/篡改/删除。 |
| PY-UNSAFE-DESERIALIZE | Python | security | high | 使用 pickle.loads 或未指定安全 Loader 的 yaml.load 反序列化外部数据：可被构造为任意代码执行。 |
| PY-WEB-NO-RATE-LIMIT | Python | security | low | 文件引入 FastAPI/Flask 路由却未 import 任何限流特征（slowapi/flask_limiter/fastapi_limiter/ratelimit/limits 等）且无 @x.limit( 用法：端点缺少速率限制，可被暴力枚举/爬取/DoS 滥用。低置信标疑——限流可能由网关/反向代理/全局中间件承担，请按部署架构甄别；每文件只报一次。 |
| PY-WEB-ROUTE-NO-AUTH | Python | security | medium | FastAPI/Flask 路由装饰器的处理函数无任何鉴权特征（装饰器无 dependencies=/Depends(/Security(，同文件无 token/session/auth/permission 校验线索，函数上方无 auth/login/required 守卫装饰器）：端点可能匿名暴露敏感操作。低置信标疑——健康检查/公开页面本就无需鉴权，请按业务语义甄别。 |

### JavaScript（24 条）

#### bug 缺陷（7 条）

| 规则 ID | 语言 | 类别 | 严重度 | 说明 |
| --- | --- | --- | --- | --- |
| JS-DEBUGGER | JavaScript | bug | high | 残留 `debugger` 语句：用户浏览器开启 DevTools 时执行到这里会强制断点，直接阻塞线上页面。 |
| JS-DOUBLE-EQ-NULL | JavaScript | bug | low | 用 `== null`/`!= null` 判空：宽松相等会把 undefined 也判进 null，而 0/''/false 又不判空，边界语义含混；应显式写 `=== null`/`=== undefined` 或收敛为统一判空工具。 |
| JS-EMPTY-CATCH | JavaScript | bug | medium | `catch` 块体为空：异常被捕获后不做任何处理，故障既无日志也无恢复动作，线上只能以『数据莫名不对』的形式暴露；至少应记录日志，无法处理时向上重新抛出。 |
| JS-EQEQEQ | JavaScript | bug | medium | 使用 `==`/`!=` 宽松比较：触发隐式类型转换（如 0==''、null==undefined 为真），逻辑易错且难排查；应使用 `===`/`!==`。 |
| JS-REASSIGN-CONST-LOOKALIKE | JavaScript | bug | low | 对 const 声明的变量再赋值：运行时抛 TypeError 'Assignment to constant variable'，属必现缺陷。 |
| JS-SETTIMEOUT-STRING | JavaScript | bug | high | setTimeout/setInterval 的回调以字符串传入：等价于隐式 eval，存在代码注入风险且无法被静态检查；应传函数。 |
| JS-VAR | JavaScript | bug | low | 使用 `var` 声明变量：作用域提升到函数级，循环闭包捕获与重复声明都会产生隐蔽缺陷；应使用 `let`/`const`。 |

#### performance 性能（2 条）

| 规则 ID | 语言 | 类别 | 严重度 | 说明 |
| --- | --- | --- | --- | --- |
| JS-AWAIT-IN-LOOP | JavaScript | performance | medium | 循环体内逐次 `await`：每次迭代都要等上一次完成，总耗时为各次之和（串行放大）；相互独立的请求应先收集 Promise 再用 `Promise.all` 并行。 |
| JS-FETCH-NO-TIMEOUT | JavaScript | performance | medium | 网络请求未设置 timeout/AbortSignal：默认无限等待，远端无响应时请求永久挂起并占用连接。 |

#### style 风格（7 条）

| 规则 ID | 语言 | 类别 | 严重度 | 说明 |
| --- | --- | --- | --- | --- |
| JS-CONSOLE-LOG | JavaScript | style | low | 使用 console.log 直接输出：绕过日志体系，无级别/上下文，常为调试残留并可能泄露内部数据。 |
| JS-DEEP-NESTING | JavaScript | style | medium | 代码嵌套达到 4 层以上：认知负担大、分支组合爆炸，应通过提前返回/抽取函数降低深度。 |
| JS-LAYER-VIOLATION | JavaScript、TypeScript | style | medium | 高层目录（api/controller 等）直接依赖低层目录（dao/repository/db 等），跳过 service 中间层：表现层与存储实现强耦合，替换存储或复用接口困难。若项目无中间层约定可忽略或调整目录命名；建议依赖经服务层转发，或以依赖倒置（接口/协议）解耦。 |
| JS-LONG-FUNCTION | JavaScript | style | medium | 函数体超过 80 行：职责过多、难以测试与复用，应拆分为更小的函数。 |
| JS-MAGIC-NUMBER | JavaScript | style | low | 代码中出现无命名的大数字面量（≥1000）：含义不明、散落多处难以统一修改，应提取为具名常量。 |
| JS-NAMING-STYLE | JavaScript | style | low | function 声明名首字母大写（PascalCase）但全文件未见 `new 该名` 调用：大写开头的函数名按 JS 惯例表示需要 new 的构造函数/类，误用会误导调用方直接调用；若确为构造函数请改用 class，否则应改为小写驼峰命名。 |
| JS-TODO-FIXME | JavaScript | style | low | 注释中的 TODO/FIXME/HACK 标记：代表已知未完成事项或临时绕过，应在交付前清理或建跟踪项。 |

#### security 安全（8 条）

| 规则 ID | 语言 | 类别 | 严重度 | 说明 |
| --- | --- | --- | --- | --- |
| JS-COMMAND-INJECTION | JavaScript、TypeScript | security | high | 通过 child_process 的 exec/execSync/spawn/spawnSync 执行拼接了变量或模板插值的命令：外部输入混入 shell 元字符（如 `; rm -rf /`）即可注入任意命令，导致远程代码执行；建议改用 execFile/数组参数形式（不经 shell 解析）并对输入做白名单校验。 |
| JS-DOCUMENT-COOKIE-WRITE | JavaScript、TypeScript | security | medium | 向 `document.cookie` 赋值变量/拼接内容：键值未经编码可能注入 `;` 破坏 cookie 结构，动态值常含凭据且对同源 JS 完全可见；请统一封装 setCookie 并做 encodeURIComponent 与 HttpOnly 评估。 |
| JS-DOCUMENT-WRITE | JavaScript、TypeScript | security | medium | 使用 document.write 输出内容：可被注入恶意 HTML（XSS），且在已加载文档上调用会清空整个页面。 |
| JS-EVAL-EXEC | JavaScript、TypeScript | security | critical | 使用 eval/new Function 动态执行代码：输入可被控制时等价于任意代码执行漏洞。 |
| JS-HARDCODED-SECRET | JavaScript、TypeScript | security | critical | 密钥/口令硬编码在源码中：随代码库扩散，任何有读权限的人都能获取凭据。 |
| JS-INNERHTML | JavaScript、TypeScript | security | high | 向 innerHTML 赋值拼接/变量内容：未转义的 HTML 会被浏览器执行，构成存储型或反射型 XSS。 |
| JS-LOCALSTORAGE-SENSITIVE | JavaScript、TypeScript | security | high | 把疑似敏感凭据（键名含 token/secret/password/key 等）写入 localStorage：localStorage 对同源任意 JS 完全开放，一个 XSS 漏洞就能把凭据读走外传；应改用 HttpOnly + Secure Cookie 或服务端会话。 |
| JS-SQL-CONCAT | JavaScript、TypeScript | security | critical | SQL 语句以字符串拼接方式引入变量：攻击者可注入任意 SQL，导致数据泄露/篡改/删除。 |

### TypeScript（16 条）

#### bug 缺陷（2 条）

| 规则 ID | 语言 | 类别 | 严重度 | 说明 |
| --- | --- | --- | --- | --- |
| TS-NEVER-ASSERT | TypeScript | bug | high | 使用 `as unknown as X` 双重断言：任何类型都能被强转成任何类型，类型系统在此处完全失效。 |
| TS-NONNULL-ABUSE | TypeScript | bug | medium | 同一文件大量使用 `!.` 非空断言：断言处的 null 检查被编译器跳过，值为空时直接运行时崩溃。 |

#### style 风格（6 条）

| 规则 ID | 语言 | 类别 | 严重度 | 说明 |
| --- | --- | --- | --- | --- |
| JS-LAYER-VIOLATION | JavaScript、TypeScript | style | medium | 高层目录（api/controller 等）直接依赖低层目录（dao/repository/db 等），跳过 service 中间层：表现层与存储实现强耦合，替换存储或复用接口困难。若项目无中间层约定可忽略或调整目录命名；建议依赖经服务层转发，或以依赖倒置（接口/协议）解耦。 |
| TS-ANY | TypeScript | style | low | 使用 any 类型：关闭该值的一切类型检查，类型错误会静默传递到运行时，逐渐腐蚀整个类型边界。 |
| TS-DEPENDS-ON-ANY | TypeScript | style | low | 函数参数使用 `any` 且未标注返回类型：返回值类型经 any 推导后失去约束，契约名存实亡；请为参数与返回值补具体类型（或 unknown + 类型收窄）。 |
| TS-EXPLICIT-ANY-PARAM | TypeScript | style | low | 导出（公共 API）函数的参数使用 any：外部调用方失去全部类型约束，API 契约名存实亡。 |
| TS-FUNC-STYLE | TypeScript | style | low | 导出（公共 API）函数缺少显式返回类型：返回值结构变化会静默传遍所有调用方，IDE 跳转与契约审查均不可用。 |
| TS-IGNORE | TypeScript | style | medium | 使用 @ts-ignore 压制类型错误：被忽略的错误仍会在运行时发生，且后续重构不会被编译器保护。 |

#### security 安全（8 条）

| 规则 ID | 语言 | 类别 | 严重度 | 说明 |
| --- | --- | --- | --- | --- |
| JS-COMMAND-INJECTION | JavaScript、TypeScript | security | high | 通过 child_process 的 exec/execSync/spawn/spawnSync 执行拼接了变量或模板插值的命令：外部输入混入 shell 元字符（如 `; rm -rf /`）即可注入任意命令，导致远程代码执行；建议改用 execFile/数组参数形式（不经 shell 解析）并对输入做白名单校验。 |
| JS-DOCUMENT-COOKIE-WRITE | JavaScript、TypeScript | security | medium | 向 `document.cookie` 赋值变量/拼接内容：键值未经编码可能注入 `;` 破坏 cookie 结构，动态值常含凭据且对同源 JS 完全可见；请统一封装 setCookie 并做 encodeURIComponent 与 HttpOnly 评估。 |
| JS-DOCUMENT-WRITE | JavaScript、TypeScript | security | medium | 使用 document.write 输出内容：可被注入恶意 HTML（XSS），且在已加载文档上调用会清空整个页面。 |
| JS-EVAL-EXEC | JavaScript、TypeScript | security | critical | 使用 eval/new Function 动态执行代码：输入可被控制时等价于任意代码执行漏洞。 |
| JS-HARDCODED-SECRET | JavaScript、TypeScript | security | critical | 密钥/口令硬编码在源码中：随代码库扩散，任何有读权限的人都能获取凭据。 |
| JS-INNERHTML | JavaScript、TypeScript | security | high | 向 innerHTML 赋值拼接/变量内容：未转义的 HTML 会被浏览器执行，构成存储型或反射型 XSS。 |
| JS-LOCALSTORAGE-SENSITIVE | JavaScript、TypeScript | security | high | 把疑似敏感凭据（键名含 token/secret/password/key 等）写入 localStorage：localStorage 对同源任意 JS 完全开放，一个 XSS 漏洞就能把凭据读走外传；应改用 HttpOnly + Secure Cookie 或服务端会话。 |
| JS-SQL-CONCAT | JavaScript、TypeScript | security | critical | SQL 语句以字符串拼接方式引入变量：攻击者可注入任意 SQL，导致数据泄露/篡改/删除。 |

### Java（3 条）

#### style 风格（1 条）

| 规则 ID | 语言 | 类别 | 严重度 | 说明 |
| --- | --- | --- | --- | --- |
| JAVA-LONG-FUNCTION | Java | style | medium | 函数体超过 80 行：职责过多、难以测试与复用，应拆分为更小的函数。 |

#### security 安全（2 条）

| 规则 ID | 语言 | 类别 | 严重度 | 说明 |
| --- | --- | --- | --- | --- |
| JAVA-HARDCODED-SECRET | Java | security | critical | 密钥/口令硬编码在源码中：随代码库扩散，任何有读权限的人都能获取凭据。 |
| JAVA-SQL-INJECTION | Java | security | critical | SQL 语句以字符串拼接方式引入变量：攻击者可注入任意 SQL，导致数据泄露/篡改/删除。 |

## 规则明细

### JAVA-HARDCODED-SECRET

- 语言：Java
- 类别：security 安全（security）
- 严重度：critical

密钥/口令硬编码在源码中：随代码库扩散，任何有读权限的人都能获取凭据。

### JAVA-LONG-FUNCTION

- 语言：Java
- 类别：style 风格（style）
- 严重度：medium

函数体超过 80 行：职责过多、难以测试与复用，应拆分为更小的函数。

### JAVA-SQL-INJECTION

- 语言：Java
- 类别：security 安全（security）
- 严重度：critical

SQL 语句以字符串拼接方式引入变量：攻击者可注入任意 SQL，导致数据泄露/篡改/删除。

### JS-AWAIT-IN-LOOP

- 语言：JavaScript
- 类别：performance 性能（performance）
- 严重度：medium

循环体内逐次 `await`：每次迭代都要等上一次完成，总耗时为各次之和（串行放大）；相互独立的请求应先收集 Promise 再用 `Promise.all` 并行。

**反例**

```javascript
async function loadAll(ids) {
  const out = [];
  for (const id of ids) {
    out.push(await fetchUser(id)); // 串行等待，n 倍时延
  }
  return out;
}
```

**正例**

```javascript
async function loadAll(ids) {
  return Promise.all(ids.map((id) => fetchUser(id)));
}
```

### JS-COMMAND-INJECTION

- 语言：JavaScript、TypeScript
- 类别：security 安全（security）
- 严重度：high

通过 child_process 的 exec/execSync/spawn/spawnSync 执行拼接了变量或模板插值的命令：外部输入混入 shell 元字符（如 `; rm -rf /`）即可注入任意命令，导致远程代码执行；建议改用 execFile/数组参数形式（不经 shell 解析）并对输入做白名单校验。

**反例**

```javascript
const cp = require('child_process');

function ping(host) {
  return cp.exec('ping -c 1 ' + host);  // host 可注入 shell 元字符
}
```

**正例**

```javascript
const { execFile } = require('child_process');

function ping(host) {
  return execFile('ping', ['-c', '1', host]);  // 数组参数不经 shell 解析
}
```

### JS-CONSOLE-LOG

- 语言：JavaScript
- 类别：style 风格（style）
- 严重度：low

使用 console.log 直接输出：绕过日志体系，无级别/上下文，常为调试残留并可能泄露内部数据。

### JS-DEBUGGER

- 语言：JavaScript
- 类别：bug 缺陷（bug）
- 严重度：high

残留 `debugger` 语句：用户浏览器开启 DevTools 时执行到这里会强制断点，直接阻塞线上页面。

### JS-DEEP-NESTING

- 语言：JavaScript
- 类别：style 风格（style）
- 严重度：medium

代码嵌套达到 4 层以上：认知负担大、分支组合爆炸，应通过提前返回/抽取函数降低深度。

### JS-DOCUMENT-COOKIE-WRITE

- 语言：JavaScript、TypeScript
- 类别：security 安全（security）
- 严重度：medium

向 `document.cookie` 赋值变量/拼接内容：键值未经编码可能注入 `;` 破坏 cookie 结构，动态值常含凭据且对同源 JS 完全可见；请统一封装 setCookie 并做 encodeURIComponent 与 HttpOnly 评估。

**反例**

```javascript
document.cookie = 'session=' + token + '; path=/';
```

**正例**

```javascript
document.cookie = `session=${encodeURIComponent(token)}; path=/; Secure; SameSite=Strict`;
```

### JS-DOCUMENT-WRITE

- 语言：JavaScript、TypeScript
- 类别：security 安全（security）
- 严重度：medium

使用 document.write 输出内容：可被注入恶意 HTML（XSS），且在已加载文档上调用会清空整个页面。

### JS-DOUBLE-EQ-NULL

- 语言：JavaScript
- 类别：bug 缺陷（bug）
- 严重度：low

用 `== null`/`!= null` 判空：宽松相等会把 undefined 也判进 null，而 0/''/false 又不判空，边界语义含混；应显式写 `=== null`/`=== undefined` 或收敛为统一判空工具。

**反例**

```javascript
if (value == null) {
  value = DEFAULT;
}
```

**正例**

```javascript
if (value === null || value === undefined) {
  value = DEFAULT;
}
```

### JS-EMPTY-CATCH

- 语言：JavaScript
- 类别：bug 缺陷（bug）
- 严重度：medium

`catch` 块体为空：异常被捕获后不做任何处理，故障既无日志也无恢复动作，线上只能以『数据莫名不对』的形式暴露；至少应记录日志，无法处理时向上重新抛出。

**反例**

```javascript
try {
  saveProfile(data);
} catch (e) {} // 失败无声无息
```

**正例**

```javascript
try {
  saveProfile(data);
} catch (e) {
  logger.error('saveProfile failed', e);
  throw e;
}
```

### JS-EQEQEQ

- 语言：JavaScript
- 类别：bug 缺陷（bug）
- 严重度：medium

使用 `==`/`!=` 宽松比较：触发隐式类型转换（如 0==''、null==undefined 为真），逻辑易错且难排查；应使用 `===`/`!==`。

### JS-EVAL-EXEC

- 语言：JavaScript、TypeScript
- 类别：security 安全（security）
- 严重度：critical

使用 eval/new Function 动态执行代码：输入可被控制时等价于任意代码执行漏洞。

### JS-FETCH-NO-TIMEOUT

- 语言：JavaScript
- 类别：performance 性能（performance）
- 严重度：medium

网络请求未设置 timeout/AbortSignal：默认无限等待，远端无响应时请求永久挂起并占用连接。

### JS-HARDCODED-SECRET

- 语言：JavaScript、TypeScript
- 类别：security 安全（security）
- 严重度：critical

密钥/口令硬编码在源码中：随代码库扩散，任何有读权限的人都能获取凭据。

### JS-INNERHTML

- 语言：JavaScript、TypeScript
- 类别：security 安全（security）
- 严重度：high

向 innerHTML 赋值拼接/变量内容：未转义的 HTML 会被浏览器执行，构成存储型或反射型 XSS。

### JS-LAYER-VIOLATION

- 语言：JavaScript、TypeScript
- 类别：style 风格（style）
- 严重度：medium

高层目录（api/controller 等）直接依赖低层目录（dao/repository/db 等），跳过 service 中间层：表现层与存储实现强耦合，替换存储或复用接口困难。若项目无中间层约定可忽略或调整目录命名；建议依赖经服务层转发，或以依赖倒置（接口/协议）解耦。

### JS-LOCALSTORAGE-SENSITIVE

- 语言：JavaScript、TypeScript
- 类别：security 安全（security）
- 严重度：high

把疑似敏感凭据（键名含 token/secret/password/key 等）写入 localStorage：localStorage 对同源任意 JS 完全开放，一个 XSS 漏洞就能把凭据读走外传；应改用 HttpOnly + Secure Cookie 或服务端会话。

**反例**

```javascript
// XSS 一旦发生，攻击者即可读走会话凭据
localStorage.setItem('access_token', token);
```

**正例**

```javascript
// HttpOnly Cookie 对 JS 不可见，XSS 无法窃取
document.cookie = `session=${encodeURIComponent(token)}; Secure; SameSite=Strict`;
```

### JS-LONG-FUNCTION

- 语言：JavaScript
- 类别：style 风格（style）
- 严重度：medium

函数体超过 80 行：职责过多、难以测试与复用，应拆分为更小的函数。

### JS-MAGIC-NUMBER

- 语言：JavaScript
- 类别：style 风格（style）
- 严重度：low

代码中出现无命名的大数字面量（≥1000）：含义不明、散落多处难以统一修改，应提取为具名常量。

### JS-NAMING-STYLE

- 语言：JavaScript
- 类别：style 风格（style）
- 严重度：low

function 声明名首字母大写（PascalCase）但全文件未见 `new 该名` 调用：大写开头的函数名按 JS 惯例表示需要 new 的构造函数/类，误用会误导调用方直接调用；若确为构造函数请改用 class，否则应改为小写驼峰命名。

**反例**

```javascript
function RenderUser(user) {
  return user.name;  // 全文件无 new RenderUser(...)，疑似普通函数
}
```

**正例**

```javascript
function renderUser(user) {
  return user.name;
}
```

### JS-REASSIGN-CONST-LOOKALIKE

- 语言：JavaScript
- 类别：bug 缺陷（bug）
- 严重度：low

对 const 声明的变量再赋值：运行时抛 TypeError 'Assignment to constant variable'，属必现缺陷。

### JS-SETTIMEOUT-STRING

- 语言：JavaScript
- 类别：bug 缺陷（bug）
- 严重度：high

setTimeout/setInterval 的回调以字符串传入：等价于隐式 eval，存在代码注入风险且无法被静态检查；应传函数。

### JS-SQL-CONCAT

- 语言：JavaScript、TypeScript
- 类别：security 安全（security）
- 严重度：critical

SQL 语句以字符串拼接方式引入变量：攻击者可注入任意 SQL，导致数据泄露/篡改/删除。

### JS-TODO-FIXME

- 语言：JavaScript
- 类别：style 风格（style）
- 严重度：low

注释中的 TODO/FIXME/HACK 标记：代表已知未完成事项或临时绕过，应在交付前清理或建跟踪项。

### JS-VAR

- 语言：JavaScript
- 类别：bug 缺陷（bug）
- 严重度：low

使用 `var` 声明变量：作用域提升到函数级，循环闭包捕获与重复声明都会产生隐蔽缺陷；应使用 `let`/`const`。

### PY-ASSERT-IN-PROD

- 语言：Python
- 类别：bug 缺陷（bug）
- 严重度：medium

生产代码使用 assert 做输入校验：python -O 运行时 assert 会被整体剥离，校验静默失效。

### PY-ASSERT-TUPLE

- 语言：Python
- 类别：bug 缺陷（bug）
- 严重度：high

if/while 的条件是带括号的元组（如 `if (a, b):`）：非空元组恒为真，分支永远执行，作者多半想写 `if a and b:` 或 `if a == (x, y):`，属必现逻辑错误。

**反例**

```python
def check(a, b):
    if (a, b):  # 恒为 True，b 的校验从未生效
        return True
    return False
```

**正例**

```python
def check(a, b):
    if a and b:
        return True
    return False
```

### PY-BARE-EXCEPT

- 语言：Python
- 类别：bug 缺陷（bug）
- 严重度：medium

使用裸 `except:` 会捕获包括 SystemExit/KeyboardInterrupt 在内的一切异常，且丢失异常上下文。

### PY-COMMAND-INJECTION

- 语言：Python
- 类别：security 安全（security）
- 严重度：high

通过 os.system/os.popen 执行拼接了变量的命令：输入含 shell 元字符时可被命令注入。亦覆盖 subprocess.Popen 动态首参或 shell=True（字符串参数会进入 shell）、os.execv* 以 sh/bash -c 调 shell，以及 run/call/check_output/Popen 列表参数经 `sh -c <动态命令>` 中转的等价注入形态。

### PY-CYCLOMATIC-COMPLEXITY

- 语言：Python
- 类别：style 风格（style）
- 严重度：medium

函数圈复杂度超过阈值：决策分支过多意味着路径组合爆炸，难以测试与维护；应拆分为更小函数，或用卫语句/字典分派降低分支。

**反例**

```python
def grade(score, bonus):
    if score < 0 or score > 100:
        return 0
    elif score >= 90 and bonus:
        return 10
    elif score >= 80 and not bonus:
        return 8
    # ……十余个分支串行排布，路径组合难以穷举
```

**正例**

```python
_GRADE_TABLE = ((90, 10), (80, 8), (60, 6))

def grade(score, bonus):
    if score < 0 or score > 100:
        return 0
    return next((v for low, v in _GRADE_TABLE if score >= low), 0) + bonus
```

### PY-DEEP-NESTING

- 语言：Python
- 类别：style 风格（style）
- 严重度：medium

代码嵌套达到 4 层以上：认知负担大、分支组合爆炸，应通过提前返回/抽取函数降低深度。

### PY-DEEPCOPY-IN-LOOP

- 语言：Python
- 类别：performance 性能（performance）
- 严重度：medium

循环内调用 copy.deepcopy：深拷贝是极重的递归操作，循环内反复执行会显著拖慢程序。

### PY-DEFAULT-CREDENTIAL

- 语言：Python
- 类别：security 安全（security）
- 严重度：high

凭据形态变量（password/token/api_key 等）被赋以知名出厂默认/弱口令（admin/123456/root 等，内置字典整词比对）：攻击者可用公开默认凭据直接登录；与 PY-HARDCODED-SECRET（高熵密钥）互补，专抓低熵默认口令。

**反例**

```python
DB_PASSWORD = "admin"          # 出厂默认口令，公开可查
AUTH = {"api_key": "123456"}  # 弱口令
if input_password == "root":
    grant_admin()
```

**正例**

```python
import os

DB_PASSWORD = os.environ["DB_PASSWORD"]  # 首次启动强制改密
AUTH = {"api_key": os.environ["API_KEY"]}
```

### PY-DYNAMIC-COMPILE

- 语言：Python
- 类别：security 安全（security）
- 严重度：medium

调用内建 compile() 且源码首参为变量/拼接（非静态字面量）：动态编译源码等价任意代码执行入口，配合 eval/exec 即可执行不可信代码。

**反例**

```python
def run(src):
    code = compile(src, "<s>", "eval")  # src 可控时等价任意代码执行
    return eval(code)
```

**正例**

```python
import ast

def read_config(src):
    return ast.literal_eval(src)  # 只解析字面量，不执行代码
```

### PY-DYNAMIC-IMPORT

- 语言：Python
- 类别：security 安全（security）
- 严重度：medium

以变量/拼接动态导入模块（__import__/importlib.import_module 首参非静态字面量）：用户可控的动态导入可被加载任意模块（恶意路径/覆盖标准库），建议白名单映射。

**反例**

```python
import importlib

def load(mod):
    return importlib.import_module(mod)  # mod 可控时可加载任意模块
```

**正例**

```python
import importlib

PLUGIN_WHITELIST = {"report": "report"}

def load(mod):
    name = PLUGIN_WHITELIST.get(mod)
    if name is None:
        raise ValueError(f"unknown plugin: {mod}")
    return importlib.import_module(name)  # 白名单映射后的可枚举导入
```

### PY-EMPTY-EXCEPT

- 语言：Python
- 类别：bug 缺陷（bug）
- 严重度：low

except 分支体只有 `...` 或字符串占位（如 docstring），异常处理逻辑尚未实现或被遗忘。

### PY-EQ-NONE

- 语言：Python
- 类别：bug 缺陷（bug）
- 严重度：low

用 `==`/`!=` 与 None 比较：重载了 __eq__ 的对象会得到错误结果，应使用 `is`/`is not`。

### PY-EVAL-EXEC

- 语言：Python
- 类别：security 安全（security）
- 严重度：critical

使用 eval/exec 动态执行代码：输入可被控制时等价于任意代码执行漏洞。

### PY-EXCEPT-PASS

- 语言：Python
- 类别：bug 缺陷（bug）
- 严重度：medium

except 分支体只有 pass：异常被静默吞掉，出错时既无日志也无恢复动作。

### PY-GLOBAL-STATE-MUTATE

- 语言：Python
- 类别：style 风格（style）
- 严重度：low

函数内通过 `global` 声明并对模块级变量赋值：写操作散落在函数间、执行顺序隐式耦合，测试互相污染且并发不安全；应改为显式传参/返回值，或收敛到类/单例中管理。

**反例**

```python
cache = {}

def reset():
    global cache
    cache = {}  # 模块状态被函数隐式改写
```

**正例**

```python
class Cache:
    def __init__(self) -> None:
        self.data: dict = {}

    def reset(self) -> None:
        self.data = {}
```

### PY-HARDCODED-SECRET

- 语言：Python
- 类别：security 安全（security）
- 严重度：critical

密钥/口令硬编码在源码中：随代码库扩散，任何有读权限的人都能获取凭据。

### PY-HARDCODED-URL

- 语言：Python
- 类别：style 风格（style）
- 严重度：low

在非常量位置硬编码 URL：环境切换（测试/预发/生产）时需改代码，应提取为配置或常量。

### PY-INDIRECT-EXEC

- 语言：Python
- 类别：security 安全（security）
- 严重度：low

getattr 参数字符串字面量含危险内建名（eval/exec/__import__ 等），或 globals()/locals() 下标取值后立即调用：间接动态执行（标疑）——静态无法确证可控性，请人工确认。

**反例**

```python
import builtins

def run(src):
    return getattr(builtins, "eval")(src)  # 间接拿到 eval，等价任意代码执行
```

**正例**

```python
OPERATIONS = {"inspect": do_inspect}  # 显式映射表，可枚举可审计

def run(kind, src):
    return OPERATIONS[kind](src)
```

### PY-IO-IN-LOOP

- 语言：Python
- 类别：performance 性能（performance）
- 严重度：high

循环体内执行文件/数据库/网络 IO：每次迭代都付出完整 IO 往返成本，应移出循环或批量处理。

### PY-IS-LITERAL

- 语言：Python
- 类别：bug 缺陷（bug）
- 严重度：low

用 `is` 与字符串/数字等字面量比较：is 是身份比较，字面量身份无保证，结果随解释器实现而异。

### PY-LAYER-VIOLATION

- 语言：Python
- 类别：style 风格（style）
- 严重度：medium

高层目录（api/controller 等）直接依赖低层目录（dao/repository/db 等），跳过 service 中间层：表现层与存储实现强耦合，替换存储或复用接口困难。若项目无中间层约定可忽略或调整目录命名；建议依赖经服务层转发，或以依赖倒置（接口/协议）解耦。

### PY-LIST-MEMBERSHIP

- 语言：Python
- 类别：performance 性能（performance）
- 严重度：medium

用 list 做 `in` 成员判断是 O(n) 线性扫描，数据量大时应改用 set/dict（O(1)）。

### PY-LOG-FORGERY

- 语言：Python
- 类别：security 安全（security）
- 严重度：low

logger/print 日志调用的格式字符串含换行转义或多行字面量，且同一调用拼接了非常量变量：外部输入可注入伪造日志行（log forging / CVE 型日志注入）；建议对用户输入做换行/控制字符清洗或改用结构化日志。

**反例**

```python
logger.info("user %s logged in\ntrace: %s", user, trace_id)
# user 输入 "x\nERROR disk full" 即可伪造一条 ERROR 日志
```

**正例**

```python
logger.info("user %s logged in trace=%s", sanitize(user), trace_id)
# 或结构化日志：logger.info("login", extra={"user": user})
```

### PY-LONG-FUNCTION

- 语言：Python
- 类别：style 风格（style）
- 严重度：medium

函数体超过 80 行：职责过多、难以测试与复用，应拆分为更小的函数。

### PY-MAGIC-NUMBER

- 语言：Python
- 类别：style 风格（style）
- 严重度：low

代码中出现无命名的大数字面量（≥1000）：含义不明、散落多处难以统一修改，应提取为具名常量。

### PY-MUTABLE-CLASS-ATTR

- 语言：Python
- 类别：bug 缺陷（bug）
- 严重度：medium

类体中以 []/{}/set() 等可变字面量定义类属性：该对象挂在类上、全部实例共享，任一实例原地修改会波及其他实例；应在 __init__ 中为每个实例创建新对象。

**反例**

```python
class Basket:
    items = []  # 所有 Basket 实例共享同一个 list

    def add(self, item):
        self.items.append(item)
```

**正例**

```python
class Basket:
    def __init__(self):
        self.items = []  # 每个实例独立

    def add(self, item):
        self.items.append(item)
```

### PY-MUTABLE-DEFAULT

- 语言：Python
- 类别：bug 缺陷（bug）
- 严重度：high

函数默认参数使用 []/{}/set() 等可变对象：默认值在多次调用间共享，跨调用累积脏数据。

### PY-NAMING-STYLE

- 语言：Python
- 类别：style 风格（style）
- 严重度：low

命名不符合 PEP 8：函数/方法名应全小写下划线分词（snake_case），类名应以大写字母开头的驼峰式（PascalCase）；命名不一致会降低可读性并让「大小写敏感的外部调用」埋下隐蔽缺陷。

**反例**

```python
def GetUserOrder(OrderTotal):
    return OrderTotal

class order_service:
    pass
```

**正例**

```python
def get_user_order(order_total):
    return order_total

class OrderService:
    pass
```

### PY-NO-TIMEOUT

- 语言：Python
- 类别：performance 性能（performance）
- 严重度：medium

网络请求未设置 timeout：默认无限等待，远端无响应时调用方将永久挂起。

### PY-NONE-DEREF

- 语言：Python
- 类别：bug 缺陷（bug）
- 严重度：medium

变量在同函数内被赋值为 None 后未经重赋值即被解引用（属性/下标/调用）：运行时必然抛出 AttributeError/TypeError；请先判空（if x is not None）、给变量赋有效值，或重构掉 None 占位状态。

**反例**

```python
def render(user):
    cache = None
    cache.get("k")  # cache 仍为 None，运行时 AttributeError
```

**正例**

```python
def render(user):
    cache = load_cache()  # 赋有效值后再使用
    cache.get("k")
```

### PY-OPEN-NO-CLOSE

- 语言：Python
- 类别：bug 缺陷（bug）
- 严重度：high

open() 的返回值既未用 with 管理，也未在作用域内显式 close：文件句柄泄漏。

### PY-OPEN-WITHOUT-ENCODING

- 语言：Python
- 类别：bug 缺陷（bug）
- 严重度：low

open() 打开文本文件未显式传 encoding=：实际编码随平台与 locale 变化（Windows 常为 GBK/cp936，Linux 为 UTF-8），同一份代码跨平台读写中文即乱码或抛 DecodeError。

**反例**

```python
with open('config.json') as f:  # Windows 上默认 GBK，遇 UTF-8 中文即崩
    data = f.read()
```

**正例**

```python
with open('config.json', encoding='utf-8') as f:
    data = f.read()
```

### PY-ORM-N-PLUS-ONE

- 语言：Python
- 类别：performance 性能（performance）
- 严重度：medium

for/while 循环体内逐条执行 ORM 查询（SQLAlchemy `session.query(X).get(/.first(/.all(` 同行链式，或 Django `Model.objects.get(`）：循环 N 次即发送 N 条 SQL（N+1 问题），数据量增大时延迟线性放大；应改为一次性批量查询或关系预加载。

**反例**

```python
for uid in user_ids:
    user = session.query(User).get(uid)  # 每轮循环发一条 SQL
    total += user.amount
```

**正例**

```python
users = session.query(User).filter(User.id.in_(user_ids)).all()  # 一条 IN 查询
by_id = {u.id: u for u in users}
for uid in user_ids:
    total += by_id[uid].amount
```

### PY-PII-LOG

- 语言：Python
- 类别：security 安全（security）
- 严重度：medium

疑似将个人敏感信息（手机号/身份证号等）写入日志且未脱敏：依据个人信息保护相关要求（如《个人信息保护法》的最小必要原则），日志中的个人信息应脱敏或最小化，明文落盘会随日志采集/归档扩散泄露面，且难以事后回收。

**反例**

```python
import logging

logger = logging.getLogger(__name__)

def log_login(user):
    logger.info("login phone=%s idcard=%s", user["phone"], user["idcard"])
```

**正例**

```python
import logging

logger = logging.getLogger(__name__)

def log_login(user):
    logger.info("login phone=%s", mask_tail(user["phone"]))  # 仅保留脱敏后的尾 4 位
```

### PY-PII-SQL

- 语言：Python
- 类别：security 安全（security）
- 严重度：medium

疑似将个人敏感信息（手机号/身份证号/邮箱等）以明文写入数据库：SQL 写库语句（INSERT INTO/UPDATE/CREATE TABLE）中出现 PII 列名，或执行 SQL 时绑定了 PII 变量。依据个人信息保护相关要求（如《个人信息保护法》的最小必要原则），敏感个人信息的存储应加密/脱敏/最小化，明文入库会随数据库、备份与导出链路长期留存并扩大泄露面。注意：本规则关注数据最小化而非 SQL 注入——参数化绑定（占位符）本身能防注入，但 PII 变量明文绑定入库仍会命中；非敏感字段的参数化查询不报。建议：对敏感字段做字段级加密、单向哈希（需检索时用 HMAC/盲索引），或仅收集与存储业务必需的最小字段。

**反例**

```python
import sqlite3


def save_user(conn, user):
    conn.execute("INSERT INTO users(phone) VALUES (?)", (user["phone"],))
```

**正例**

```python
import sqlite3


def save_user(conn, user):
    # 敏感字段先脱敏/加密再入库：手机号仅存掩码（保留尾 4 位），检索走哈希盲索引
    conn.execute("INSERT INTO users(contact_masked) VALUES (?)", (mask_tail(user["contact"]),))
```

### PY-PINYIN-NAMING

- 语言：Python
- 类别：style 风格（style）
- 严重度：low

标识符疑似拼音命名（如 jieguo/jisuan/shuju）：中英混用的拼音命名会显著降低代码可读性与可检索性，团队协作时难以理解意图；建议统一改用英文命名。本条为保守启发式，仅对可完整拆分为 ≥2 个常见拼音音节且非英文词的标识符提示。

**反例**

```python
def jisuan_heji(shuju):
    jieguo = sum(shuju)
    return jieguo
```

**正例**

```python
def calculate_total(values):
    total = sum(values)
    return total
```

### PY-PRINT-DEBUG

- 语言：Python
- 类别：style 风格（style）
- 严重度：low

生产代码中使用 print 输出：绕过日志体系，无级别/时间/上下文，且影响性能与日志采集。

### PY-REPEAT-CALL

- 语言：Python
- 类别：performance 性能（performance）
- 严重度：low

同一函数内重复执行参数完全相同的函数调用：若调用非幂等或有开销，应提取为局部变量复用。

### PY-RETURN-IN-INIT

- 语言：Python
- 类别：bug 缺陷（bug）
- 严重度：high

__init__ 中 `return` 了非 None 值：Python 规定构造器必须返回 None，实例化时直接抛 `TypeError: __init__() should return None`，属必现运行时错误。

**反例**

```python
class Client:
    def __init__(self, cfg):
        self.cfg = cfg
        return self  # TypeError: __init__() should return None
```

**正例**

```python
class Client:
    def __init__(self, cfg):
        self.cfg = cfg
```

### PY-SHADOW-BUILTIN

- 语言：Python
- 类别：bug 缺陷（bug）
- 严重度：low

把 list/dict/str 等内置名用作变量名：遮蔽内置函数，后续同作用域调用原始内置将直接报错。

### PY-SLEEP-IN-ASYNC

- 语言：Python
- 类别：performance 性能（performance）
- 严重度：medium

async def 体内调用同步 time.sleep：阻塞整个事件循环，期间所有协程（含其他请求）都无法调度，异步吞吐骤降；应改用 `await asyncio.sleep(...)`。

**反例**

```python
import time

async def poll():
    while True:
        time.sleep(1)  # 事件循环被阻塞 1 秒，其他协程全部停摆
        await tick()
```

**正例**

```python
import asyncio

async def poll():
    while True:
        await asyncio.sleep(1)  # 异步让出，事件循环可调度其他协程
        await tick()
```

### PY-SQL-INJECTION

- 语言：Python
- 类别：security 安全（security）
- 严重度：critical

SQL 语句以字符串拼接方式引入外部输入：攻击者可注入任意 SQL，导致数据泄露/篡改/删除。

### PY-STR-CONCAT-LOOP

- 语言：Python
- 类别：performance 性能（performance）
- 严重度：low

循环内对字符串做 +/+= 拼接：每次都生成新字符串对象，总体 O(n²)；应收集到列表后 ''.join()。

### PY-STRING-FORMAT-IN-LOGGING

- 语言：Python
- 类别：performance 性能（performance）
- 严重度：low

在 logging 调用里用 %/.format/f-string 预先拼好消息：即使该级别被过滤也会付出格式化成本，且丢失日志聚合字段；应改用惰性参数化 `logger.info('value: %s', x)`。

**反例**

```python
import logging

logger = logging.getLogger(__name__)
logger.debug('processed %d items' % n)      # 关闭 debug 也执行格式化
logger.info('user {}'.format(user))
logger.warning(f'latency {latency}ms')
```

**正例**

```python
import logging

logger = logging.getLogger(__name__)
logger.debug('processed %d items', n)
logger.info('user %s', user)
logger.warning('latency %sms', latency)
```

### PY-SUBPROCESS-WITHOUT-CHECK

- 语言：Python
- 类别：bug 缺陷（bug）
- 严重度：high

subprocess.run/call 未设置 check=True：命令非零退出不抛异常，失败被静默吞掉，后续步骤在错误前提下继续执行；应显式传 check=True 或自行检查 returncode。

**反例**

```python
import subprocess

subprocess.run(['git', 'push', 'origin', 'main'])  # 推送失败也无感知
```

**正例**

```python
import subprocess

subprocess.run(['git', 'push', 'origin', 'main'], check=True)  # 失败即抛 CalledProcessError
```

### PY-TODO-FIXME

- 语言：Python
- 类别：style 风格（style）
- 严重度：low

注释中的 TODO/FIXME/HACK 标记：代表已知未完成事项或临时绕过，应在交付前清理或建跟踪项。

### PY-TYPE-COMPARE

- 语言：Python
- 类别：style 风格（style）
- 严重度：low

用 `type(x) ==/is ...` 做类型判断：绕开继承体系（子类实例判为不等），且绕过 `__eq__` 语义；应改用 `isinstance(x, T)`。

**反例**

```python
if type(payload) == dict:  # 子类实例会判为 False
    handle(payload)
```

**正例**

```python
if isinstance(payload, dict):
    handle(payload)
```

### PY-UNREACHABLE-CODE

- 语言：Python
- 类别：bug 缺陷（bug）
- 严重度：medium

控制流终止语句（return/raise/break/continue）之后存在同缩进语句：永远不可达，多为逻辑错误或死代码。

### PY-UNSAFE-DESERIALIZE

- 语言：Python
- 类别：security 安全（security）
- 严重度：high

使用 pickle.loads 或未指定安全 Loader 的 yaml.load 反序列化外部数据：可被构造为任意代码执行。

### PY-UNSYNCED-SHARED-MUTATION

- 语言：Python
- 类别：bug 缺陷（bug）
- 严重度：medium

文件导入 threading/multiprocessing 且存在模块级可变变量（[]/{}/set()/0），函数内对其做增强赋值或 append/add/update 变异却无锁保护：多线程并发时读-改-写交错会丢失更新；应用 Lock 保护临界区、改用 queue.Queue 或原子操作。疑似级启发：静态无法证明多线程实际调用。

**反例**

```python
import threading

counter = 0

def worker():
    global counter
    counter += 1  # 多线程同时执行时丢失更新
```

**正例**

```python
import threading

counter = 0
lock = threading.Lock()

def worker():
    global counter
    with lock:  # 临界区受锁保护
        counter += 1
```

### PY-WEB-NO-RATE-LIMIT

- 语言：Python
- 类别：security 安全（security）
- 严重度：low

文件引入 FastAPI/Flask 路由却未 import 任何限流特征（slowapi/flask_limiter/fastapi_limiter/ratelimit/limits 等）且无 @x.limit( 用法：端点缺少速率限制，可被暴力枚举/爬取/DoS 滥用。低置信标疑——限流可能由网关/反向代理/全局中间件承担，请按部署架构甄别；每文件只报一次。

**反例**

```python
from fastapi import FastAPI

app = FastAPI()

@app.post("/login")  # 登录端点无限流：可被暴力破解
def login(form: LoginForm):
    ...
```

**正例**

```python
from fastapi import FastAPI
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
app = FastAPI()

@app.post("/login")
@limiter.limit("5/minute")  # 按来源 IP 限流
def login(form: LoginForm):
    ...
```

### PY-WEB-ROUTE-NO-AUTH

- 语言：Python
- 类别：security 安全（security）
- 严重度：medium

FastAPI/Flask 路由装饰器的处理函数无任何鉴权特征（装饰器无 dependencies=/Depends(/Security(，同文件无 token/session/auth/permission 校验线索，函数上方无 auth/login/required 守卫装饰器）：端点可能匿名暴露敏感操作。低置信标疑——健康检查/公开页面本就无需鉴权，请按业务语义甄别。

**反例**

```python
from fastapi import FastAPI

app = FastAPI()

@app.delete("/users/{uid}")  # 删用户接口无任何鉴权
def delete_user(uid: int):
    db.delete_user(uid)
```

**正例**

```python
from fastapi import FastAPI, Depends

app = FastAPI()

@app.delete("/users/{uid}", dependencies=[Depends(require_admin)])
def delete_user(uid: int):
    db.delete_user(uid)
```

### TS-ANY

- 语言：TypeScript
- 类别：style 风格（style）
- 严重度：low

使用 any 类型：关闭该值的一切类型检查，类型错误会静默传递到运行时，逐渐腐蚀整个类型边界。

### TS-DEPENDS-ON-ANY

- 语言：TypeScript
- 类别：style 风格（style）
- 严重度：low

函数参数使用 `any` 且未标注返回类型：返回值类型经 any 推导后失去约束，契约名存实亡；请为参数与返回值补具体类型（或 unknown + 类型收窄）。

**反例**

```typescript
function transform(raw: any) { // 返回类型缺省，随 any 一并失守
  return JSON.parse(raw);
}
```

**正例**

```typescript
function transform(raw: string): Record<string, unknown> {
  return JSON.parse(raw) as Record<string, unknown>;
}
```

### TS-EXPLICIT-ANY-PARAM

- 语言：TypeScript
- 类别：style 风格（style）
- 严重度：low

导出（公共 API）函数的参数使用 any：外部调用方失去全部类型约束，API 契约名存实亡。

### TS-FUNC-STYLE

- 语言：TypeScript
- 类别：style 风格（style）
- 严重度：low

导出（公共 API）函数缺少显式返回类型：返回值结构变化会静默传遍所有调用方，IDE 跳转与契约审查均不可用。

### TS-IGNORE

- 语言：TypeScript
- 类别：style 风格（style）
- 严重度：medium

使用 @ts-ignore 压制类型错误：被忽略的错误仍会在运行时发生，且后续重构不会被编译器保护。

### TS-NEVER-ASSERT

- 语言：TypeScript
- 类别：bug 缺陷（bug）
- 严重度：high

使用 `as unknown as X` 双重断言：任何类型都能被强转成任何类型，类型系统在此处完全失效。

### TS-NONNULL-ABUSE

- 语言：TypeScript
- 类别：bug 缺陷（bug）
- 严重度：medium

同一文件大量使用 `!.` 非空断言：断言处的 null 检查被编译器跳过，值为空时直接运行时崩溃。

## 统计

| 维度 | 分布 |
| --- | --- |
| 规则总数 | 86 |
| 按语言 | Python 52 / JavaScript 24 / TypeScript 16 / Java 3（JS/TS 共享规则在两种语言下重复计数） | |
| 按类别 | bug 26 / performance 11 / style 25 / security 24 |
| 按严重度 | critical 8 / high 15 / medium 33 / low 30 |
