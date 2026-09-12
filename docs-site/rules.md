# 规则手册

> 本页由 `scripts/gen_rule_docs.py` 从规则注册表（audit/detect/registry.py 的 DEFAULT_REGISTRY）自动生成——请勿手改；新增或调整规则后重新运行即可。

当前共 **63** 条内置规则；JS/TS 共享规则（security 类）同时作用于两种语言。

## 规则总表

### Python（35 条）

#### bug 缺陷（15 条）

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
| PY-OPEN-NO-CLOSE | Python | bug | high | open() 的返回值既未用 with 管理，也未在作用域内显式 close：文件句柄泄漏。 |
| PY-OPEN-WITHOUT-ENCODING | Python | bug | low | open() 打开文本文件未显式传 encoding=：实际编码随平台与 locale 变化（Windows 常为 GBK/cp936，Linux 为 UTF-8），同一份代码跨平台读写中文即乱码或抛 DecodeError。 |
| PY-RETURN-IN-INIT | Python | bug | high | __init__ 中 `return` 了非 None 值：Python 规定构造器必须返回 None，实例化时直接抛 `TypeError: __init__() should return None`，属必现运行时错误。 |
| PY-SHADOW-BUILTIN | Python | bug | low | 把 list/dict/str 等内置名用作变量名：遮蔽内置函数，后续同作用域调用原始内置将直接报错。 |
| PY-SUBPROCESS-WITHOUT-CHECK | Python | bug | high | subprocess.run/call 未设置 check=True：命令非零退出不抛异常，失败被静默吞掉，后续步骤在错误前提下继续执行；应显式传 check=True 或自行检查 returncode。 |
| PY-UNREACHABLE-CODE | Python | bug | medium | 控制流终止语句（return/raise/break/continue）之后存在同缩进语句：永远不可达，多为逻辑错误或死代码。 |

#### performance 性能（7 条）

| 规则 ID | 语言 | 类别 | 严重度 | 说明 |
| --- | --- | --- | --- | --- |
| PY-DEEPCOPY-IN-LOOP | Python | performance | medium | 循环内调用 copy.deepcopy：深拷贝是极重的递归操作，循环内反复执行会显著拖慢程序。 |
| PY-IO-IN-LOOP | Python | performance | high | 循环体内执行文件/数据库/网络 IO：每次迭代都付出完整 IO 往返成本，应移出循环或批量处理。 |
| PY-LIST-MEMBERSHIP | Python | performance | medium | 用 list 做 `in` 成员判断是 O(n) 线性扫描，数据量大时应改用 set/dict（O(1)）。 |
| PY-NO-TIMEOUT | Python | performance | medium | 网络请求未设置 timeout：默认无限等待，远端无响应时调用方将永久挂起。 |
| PY-REPEAT-CALL | Python | performance | low | 同一函数内重复执行参数完全相同的函数调用：若调用非幂等或有开销，应提取为局部变量复用。 |
| PY-STR-CONCAT-LOOP | Python | performance | low | 循环内对字符串做 +/+= 拼接：每次都生成新字符串对象，总体 O(n²)；应收集到列表后 ''.join()。 |
| PY-STRING-FORMAT-IN-LOGGING | Python | performance | low | 在 logging 调用里用 %/.format/f-string 预先拼好消息：即使该级别被过滤也会付出格式化成本，且丢失日志聚合字段；应改用惰性参数化 `logger.info('value: %s', x)`。 |

#### style 风格（8 条）

| 规则 ID | 语言 | 类别 | 严重度 | 说明 |
| --- | --- | --- | --- | --- |
| PY-DEEP-NESTING | Python | style | medium | 代码嵌套达到 4 层以上：认知负担大、分支组合爆炸，应通过提前返回/抽取函数降低深度。 |
| PY-GLOBAL-STATE-MUTATE | Python | style | low | 函数内通过 `global` 声明并对模块级变量赋值：写操作散落在函数间、执行顺序隐式耦合，测试互相污染且并发不安全；应改为显式传参/返回值，或收敛到类/单例中管理。 |
| PY-HARDCODED-URL | Python | style | low | 在非常量位置硬编码 URL：环境切换（测试/预发/生产）时需改代码，应提取为配置或常量。 |
| PY-LONG-FUNCTION | Python | style | medium | 函数体超过 80 行：职责过多、难以测试与复用，应拆分为更小的函数。 |
| PY-MAGIC-NUMBER | Python | style | low | 代码中出现无命名的大数字面量（≥1000）：含义不明、散落多处难以统一修改，应提取为具名常量。 |
| PY-PRINT-DEBUG | Python | style | low | 生产代码中使用 print 输出：绕过日志体系，无级别/时间/上下文，且影响性能与日志采集。 |
| PY-TODO-FIXME | Python | style | low | 注释中的 TODO/FIXME/HACK 标记：代表已知未完成事项或临时绕过，应在交付前清理或建跟踪项。 |
| PY-TYPE-COMPARE | Python | style | low | 用 `type(x) ==/is ...` 做类型判断：绕开继承体系（子类实例判为不等），且绕过 `__eq__` 语义；应改用 `isinstance(x, T)`。 |

#### security 安全（5 条）

| 规则 ID | 语言 | 类别 | 严重度 | 说明 |
| --- | --- | --- | --- | --- |
| PY-COMMAND-INJECTION | Python | security | high | 通过 os.system/os.popen 执行拼接了变量的命令：输入含 shell 元字符时可被命令注入。 |
| PY-EVAL-EXEC | Python | security | critical | 使用 eval/exec 动态执行代码：输入可被控制时等价于任意代码执行漏洞。 |
| PY-HARDCODED-SECRET | Python | security | critical | 密钥/口令硬编码在源码中：随代码库扩散，任何有读权限的人都能获取凭据。 |
| PY-SQL-INJECTION | Python | security | critical | SQL 语句以字符串拼接方式引入外部输入：攻击者可注入任意 SQL，导致数据泄露/篡改/删除。 |
| PY-UNSAFE-DESERIALIZE | Python | security | high | 使用 pickle.loads 或未指定安全 Loader 的 yaml.load 反序列化外部数据：可被构造为任意代码执行。 |

### JavaScript（21 条）

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

#### style 风格（5 条）

| 规则 ID | 语言 | 类别 | 严重度 | 说明 |
| --- | --- | --- | --- | --- |
| JS-CONSOLE-LOG | JavaScript | style | low | 使用 console.log 直接输出：绕过日志体系，无级别/上下文，常为调试残留并可能泄露内部数据。 |
| JS-DEEP-NESTING | JavaScript | style | medium | 代码嵌套达到 4 层以上：认知负担大、分支组合爆炸，应通过提前返回/抽取函数降低深度。 |
| JS-LONG-FUNCTION | JavaScript | style | medium | 函数体超过 80 行：职责过多、难以测试与复用，应拆分为更小的函数。 |
| JS-MAGIC-NUMBER | JavaScript | style | low | 代码中出现无命名的大数字面量（≥1000）：含义不明、散落多处难以统一修改，应提取为具名常量。 |
| JS-TODO-FIXME | JavaScript | style | low | 注释中的 TODO/FIXME/HACK 标记：代表已知未完成事项或临时绕过，应在交付前清理或建跟踪项。 |

#### security 安全（7 条）

| 规则 ID | 语言 | 类别 | 严重度 | 说明 |
| --- | --- | --- | --- | --- |
| JS-DOCUMENT-COOKIE-WRITE | JavaScript、TypeScript | security | medium | 向 `document.cookie` 赋值变量/拼接内容：键值未经编码可能注入 `;` 破坏 cookie 结构，动态值常含凭据且对同源 JS 完全可见；请统一封装 setCookie 并做 encodeURIComponent 与 HttpOnly 评估。 |
| JS-DOCUMENT-WRITE | JavaScript、TypeScript | security | medium | 使用 document.write 输出内容：可被注入恶意 HTML（XSS），且在已加载文档上调用会清空整个页面。 |
| JS-EVAL-EXEC | JavaScript、TypeScript | security | critical | 使用 eval/new Function 动态执行代码：输入可被控制时等价于任意代码执行漏洞。 |
| JS-HARDCODED-SECRET | JavaScript、TypeScript | security | critical | 密钥/口令硬编码在源码中：随代码库扩散，任何有读权限的人都能获取凭据。 |
| JS-INNERHTML | JavaScript、TypeScript | security | high | 向 innerHTML 赋值拼接/变量内容：未转义的 HTML 会被浏览器执行，构成存储型或反射型 XSS。 |
| JS-LOCALSTORAGE-SENSITIVE | JavaScript、TypeScript | security | high | 把疑似敏感凭据（键名含 token/secret/password/key 等）写入 localStorage：localStorage 对同源任意 JS 完全开放，一个 XSS 漏洞就能把凭据读走外传；应改用 HttpOnly + Secure Cookie 或服务端会话。 |
| JS-SQL-CONCAT | JavaScript、TypeScript | security | critical | SQL 语句以字符串拼接方式引入变量：攻击者可注入任意 SQL，导致数据泄露/篡改/删除。 |

### TypeScript（14 条）

#### bug 缺陷（2 条）

| 规则 ID | 语言 | 类别 | 严重度 | 说明 |
| --- | --- | --- | --- | --- |
| TS-NEVER-ASSERT | TypeScript | bug | high | 使用 `as unknown as X` 双重断言：任何类型都能被强转成任何类型，类型系统在此处完全失效。 |
| TS-NONNULL-ABUSE | TypeScript | bug | medium | 同一文件大量使用 `!.` 非空断言：断言处的 null 检查被编译器跳过，值为空时直接运行时崩溃。 |

#### style 风格（5 条）

| 规则 ID | 语言 | 类别 | 严重度 | 说明 |
| --- | --- | --- | --- | --- |
| TS-ANY | TypeScript | style | low | 使用 any 类型：关闭该值的一切类型检查，类型错误会静默传递到运行时，逐渐腐蚀整个类型边界。 |
| TS-DEPENDS-ON-ANY | TypeScript | style | low | 函数参数使用 `any` 且未标注返回类型：返回值类型经 any 推导后失去约束，契约名存实亡；请为参数与返回值补具体类型（或 unknown + 类型收窄）。 |
| TS-EXPLICIT-ANY-PARAM | TypeScript | style | low | 导出（公共 API）函数的参数使用 any：外部调用方失去全部类型约束，API 契约名存实亡。 |
| TS-FUNC-STYLE | TypeScript | style | low | 导出（公共 API）函数缺少显式返回类型：返回值结构变化会静默传遍所有调用方，IDE 跳转与契约审查均不可用。 |
| TS-IGNORE | TypeScript | style | medium | 使用 @ts-ignore 压制类型错误：被忽略的错误仍会在运行时发生，且后续重构不会被编译器保护。 |

#### security 安全（7 条）

| 规则 ID | 语言 | 类别 | 严重度 | 说明 |
| --- | --- | --- | --- | --- |
| JS-DOCUMENT-COOKIE-WRITE | JavaScript、TypeScript | security | medium | 向 `document.cookie` 赋值变量/拼接内容：键值未经编码可能注入 `;` 破坏 cookie 结构，动态值常含凭据且对同源 JS 完全可见；请统一封装 setCookie 并做 encodeURIComponent 与 HttpOnly 评估。 |
| JS-DOCUMENT-WRITE | JavaScript、TypeScript | security | medium | 使用 document.write 输出内容：可被注入恶意 HTML（XSS），且在已加载文档上调用会清空整个页面。 |
| JS-EVAL-EXEC | JavaScript、TypeScript | security | critical | 使用 eval/new Function 动态执行代码：输入可被控制时等价于任意代码执行漏洞。 |
| JS-HARDCODED-SECRET | JavaScript、TypeScript | security | critical | 密钥/口令硬编码在源码中：随代码库扩散，任何有读权限的人都能获取凭据。 |
| JS-INNERHTML | JavaScript、TypeScript | security | high | 向 innerHTML 赋值拼接/变量内容：未转义的 HTML 会被浏览器执行，构成存储型或反射型 XSS。 |
| JS-LOCALSTORAGE-SENSITIVE | JavaScript、TypeScript | security | high | 把疑似敏感凭据（键名含 token/secret/password/key 等）写入 localStorage：localStorage 对同源任意 JS 完全开放，一个 XSS 漏洞就能把凭据读走外传；应改用 HttpOnly + Secure Cookie 或服务端会话。 |
| JS-SQL-CONCAT | JavaScript、TypeScript | security | critical | SQL 语句以字符串拼接方式引入变量：攻击者可注入任意 SQL，导致数据泄露/篡改/删除。 |

## 规则明细

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

通过 os.system/os.popen 执行拼接了变量的命令：输入含 shell 元字符时可被命令注入。

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

### PY-LIST-MEMBERSHIP

- 语言：Python
- 类别：performance 性能（performance）
- 严重度：medium

用 list 做 `in` 成员判断是 O(n) 线性扫描，数据量大时应改用 set/dict（O(1)）。

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

### PY-NO-TIMEOUT

- 语言：Python
- 类别：performance 性能（performance）
- 严重度：medium

网络请求未设置 timeout：默认无限等待，远端无响应时调用方将永久挂起。

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
| 规则总数 | 63 |
| 按语言 | Python 35 / JavaScript 21 / TypeScript 14（JS/TS 共享规则在两种语言下重复计数） | |
| 按类别 | bug 24 / performance 9 / style 18 / security 12 |
| 按严重度 | critical 6 / high 13 / medium 20 / low 24 |
