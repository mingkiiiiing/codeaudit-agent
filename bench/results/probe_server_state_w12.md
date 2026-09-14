# W12-A4 服务端层进程内状态探针报告（bench/memdiag/probe_server_state.py）

- 生成时间：2026-09-15T00:27:00
- Python：3.13.9；被测：server/app.py（同进程双形态：uvicorn 子线程 + ASGI transport）
- 任务素材：demo/mini_app；阶段1 并发 8 个（闸门容量 4，含排队）+ SSE 断开 3 轮
- 离线：进程 pop GLM_*；db 与工作目录在系统临时目录（finally 已清理）

## 逐嫌疑取证

| 嫌疑点 | 结果 | 证据 |
|---|---|---|
| _RUNNING 句柄清理 | 通过 | 8 任务（含排队）终态后 len(_RUNNING)=0 |
| _TASKS 弱引用集清理 | 通过 | 终态后 len(_TASKS)=0 |
| _RUN_GATE 信号量配对 | 通过 | 排队路径终态后 _value=4（初始 4） |
| 离线断言（api_key 脱敏 + FakeLLM 零用量 + 无 .env） | 通过 | 8 任务：api_key 违例=0，tokens 非零违例=0，服务 CWD 存在 .env=False |
| SSE 客户端断开后生成器释放 | 通过 | 3 次中途断开；断开前存活 event_stream 生成器=0，断开后=0（sse_starlette 3.4.11 cancel_on_finish + _listen_for_disconnect：断开即取消 task group） |
| SSE 正常收流后生成器关闭 | 通过 | done 终帧=True；残留 event_stream 生成器=0 |
| events 表 DELETE 归零 | 通过 | 任务存活期 events 行数=264；全量 DELETE 后 events=0，audits=0（get_events 按 audit_id 过滤读，单任务事件数有界 → 读路径内存不受表总量影响） |
| store 单例连接复用 | 通过 | 静默期存活 sqlite3.Connection=1（应为 1：_STORE 单例）；单例判定=True |
| 游标无钉死 | 通过 | 静默期存活 sqlite3.Cursor=0（每查询 execute 新建游标，用后引用计数回收） |
| 请求日志缓冲（uvicorn/FastAPI） | 通过 | logging handler 数：负载前=0，负载后=2（含 uvicorn 启动期配置）；内存缓冲型 handler（Memory/Queue/Buffering）=0——uvicorn 为写透型 StreamHandler，无内存缓冲累积；线程数 1→1（增量=to_thread 默认线程池惰性扩容，上限 min(32, cpu+4)，有界） |

## 结论

- 共 10 项取证，异常 0 项：server/app.py 簿记嫌疑全部排除。
- 静态佐证：done callback 对 _TASKS.discard 与 _RUNNING.pop 成对执行且含 is 判守卫（server/app.py:250-256，add_done_callback 在任务取消/异常时同样触发）；_run_audit_task 以 acquired 标志 + finally release 严格配对（217-225，acquire 未返回时无超额释放）；TaskStore 单连接复用（taskstore.py:84），get/list/get_events 均 fetchall 后行集即刻可回收；delete/prune 均连带清理 events（taskstore.py:172-178/304）——events 表只随存活任务增长；sse_starlette 3.4.11 __call__ 在断开/收流结束时 cancel 整个 task group（生成器 aclose，游标为局部列表）。
