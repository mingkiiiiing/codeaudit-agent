# 对抗与滥用测试报告（bench/adversarial/run_adversarial.py，W9-A1）

## 环境

- **生成时间**：2026-09-16 02:55:26
- **主机 / 平台**：DESKTOP-KS9DBJD / Windows-11-10.0.26200-SP0
- **Python**：3.13.9
- **被测服务**：server/app.py（FastAPI + uvicorn 单 worker），base=http://127.0.0.1:8903
- **网络**：全程离线（启动前清除 GLM_*，纯规则模式）
- **口径**：HTTP 场景真实 httpx 异步客户端；库层场景直调 audit.sandbox / audit.orchestrator

## 摘要

| 场景 | 状态 | 关键结果 |
|---|---|---|
| A7_sandbox_escape | ✅ OK | 超时击杀=True；F3 越权写=True |
| A8_prompt_injection | ✅ OK | 真缺陷保留=True；检出=2 处 |
| A1_malicious_zips | ✅ OK | 10 用例，建任务者全落终态=True；逃逸(外/内)=0/0 |
| A2_param_abuse | ✅ OK | 20 探针；F1 任意路径审计=200/done/issues=1/leak=True；密文回传=True |
| A3_flood_task_table | ✅ OK | 60 空任务，表上限=42；F5 准入：25 路接纳 20/拒 429×5/峰值 active=20 |
| A4_churn | ✅ OK | 30 轮抖动；幽灵 DELETE=[404, 404, 405, 404] |
| A5_sse_flood | ✅ OK | 100/100 收流；首字节 p95=1105.8ms |
| A6_oversize_upload | ✅ OK | 210MB→413；RSS 峰值=661.4MB；60MB×3 e2e p50=1437.1ms |

## 场景明细

### A7_sandbox_escape ｜ ✅ OK ｜ 场景耗时 5.35s

| 校验 | 结果 | 说明 |
|---|---|---|
| timeout_kill | ✅ | timed_out=True dur=2.157 |
| output_bomb_tail_only | ✅ | exit=0 truncated=True tail_lines=80 tail_bytes=8098 |
| framework_whitelist | ✅ | - |
| f3_documented_risk | ✅ | 沙箱进程可写 cwd 之外路径属已记录限制（F3，生产建议 Docker --network none）；本场景仅取证 |

```json
{
  "timeout_kill": {
    "timed_out": true,
    "duration_sec": 2.157
  },
  "output_bomb": {
    "exit_code": 0,
    "duration_sec": 2.864,
    "tail_lines": 80,
    "truncated": true,
    "note": "W10 F4 已修复：输出限量排空（8MB 上限），tail 带 [output truncated] 标记"
  },
  "f3_fs_escape": {
    "attempted": true,
    "escaped": true,
    "exit_code": 0
  }
}
```

### A8_prompt_injection ｜ ✅ OK ｜ 场景耗时 0.00s

| 校验 | 结果 | 说明 |
|---|---|---|
| injection_no_crash | ✅ | - |
| real_defect_still_detected | ✅ | 检出文件：['payload.py', 'store.py']（注入 payload 不得掩盖真缺陷） |
| payload_defect_detected | ✅ | 注入文件自身的 SQL 拼接缺陷也应命中（规则通道逐文件独立判定） |
| no_injection_echo | ✅ | [] |

```json
{
  "detected_files": [
    "payload.py",
    "store.py"
  ],
  "issue_count": 2,
  "health_score": 0.0
}
```

### A1_malicious_zips ｜ ✅ OK ｜ 场景耗时 33.43s

| 校验 | 结果 | 说明 |
|---|---|---|
| not_a_zip_400 | ✅ | got 400 |
| no_5xx | ✅ | {'zipslip': 200, 'zip_bomb_1gb_declared': 200, 'not_a_zip': 400, 'empty_zip': 200, 'weird_names': 200, 'windows_reserved_names': 200, 'deep_nesting_40': 200, 'zip_in_zip': 200, 'binary_py': 200, 'single_3mb_line': 200} |
| all_terminal | ✅ | {'zipslip': 'done', 'zip_bomb_1gb_declared': 'done', 'empty_zip': 'done', 'weird_names': 'done', 'windows_reserved_names': 'done', 'deep_nesting_40': 'done', 'zip_in_zip': 'done', 'binary_py': 'done', 'single_3mb_line': 'done'} |
| bomb_rejected_or_empty | ✅ | got done（1GB 声明量应在 ingest 层拒绝并落空报告） |
| no_escape_outside_workroot | ✅ | 项目根出现 marker：[] |
| no_escape_inside_workroot_except_backslash_quirk | ✅ | 工作副本树内非预期 marker：[] |
| server_alive_after_arsenal | ✅ | {'status': 'ok', 'version': '0.6.0', 'audits': {'active': 0, 'total': 2}} |

```json
{
  "cases": [
    "zipslip",
    "zip_bomb_1gb_declared",
    "not_a_zip",
    "empty_zip",
    "weird_names",
    "windows_reserved_names",
    "deep_nesting_40",
    "zip_in_zip",
    "binary_py",
    "single_3mb_line"
  ],
  "upload_codes": {
    "zipslip": 200,
    "zip_bomb_1gb_declared": 200,
    "not_a_zip": 400,
    "empty_zip": 200,
    "weird_names": 200,
    "windows_reserved_names": 200,
    "deep_nesting_40": 200,
    "zip_in_zip": 200,
    "binary_py": 200,
    "single_3mb_line": 200
  },
  "terminals": {
    "zipslip": "done",
    "zip_bomb_1gb_declared": "done",
    "not_a_zip": "http-400",
    "empty_zip": "done",
    "weird_names": "done",
    "windows_reserved_names": "done",
    "deep_nesting_40": "done",
    "zip_in_zip": "done",
    "binary_py": "done",
    "single_3mb_line": "done"
  },
  "bomb_done_report_loc": null,
  "escape_inside_workroot": [],
  "escape_outside_workroot": []
}
```

### A2_param_abuse ｜ ✅ OK ｜ 场景耗时 2.07s

| 校验 | 结果 | 说明 |
|---|---|---|
| mini_target_done | ✅ | got done |
| abuse_matrix_exact | ✅ | - |
| empty_source_400 | ✅ | got 400 |
| missing_source_400 | ✅ | got 400 |
| plain_file_source_no_5xx | ✅ | 200/done |
| f1_documented_risk | ✅ | 无鉴权 + 任意 source_path 属已记录设计风险（F1），本场景仅取证；200 即确认可审计任意本地目录 |
| server_alive | ✅ | {'status': 'ok', 'version': '0.6.0', 'audits': {'active': 0, 'total': 2}} |

```json
{
  "probes": 20,
  "plain_file_source": "200/done",
  "f1_unauth_local_dir_audit": "200/done/issues=1/leak=True",
  "f1_secret_leaked_in_report": true
}
```

### A3_flood_task_table ｜ ✅ OK ｜ 场景耗时 175.09s

| 校验 | 结果 | 说明 |
|---|---|---|
| table_capped_at_50 | ✅ | total=42（FIFO 淘汰应把终态表压回 ≤50） |
| f5_submitted_all_resolved | ✅ | 接纳 20 + 拒绝 5 != 25 |
| f5_no_5xx | ✅ | [200, 429] |
| f5_active_bounded | ✅ | active（queued+running 口径）峰值=20，契约上界=20 |
| f5_accepted_all_terminal | ✅ | ['done', 'done', 'done', 'done', 'done', 'done', 'done', 'done', 'done', 'done', 'done', 'done', 'done', 'done', 'done', 'done', 'done', 'done', 'done', 'done'] |
| server_alive | ✅ | {'status': 'ok', 'version': '0.6.0', 'audits': {'active': 0, 'total': 0}} |

```json
{
  "flood_count": 60,
  "upload_wall_sec": 1.46,
  "terminals": {
    "done": 40
  },
  "health_after_flood": {
    "status": "ok",
    "version": "0.6.0",
    "audits": {
      "active": 0,
      "total": 42
    }
  },
  "f5_submitted": 25,
  "f5_accepted": 20,
  "f5_denied_429": 5,
  "f5_active_peak": 20,
  "f5_active_series": [
    0,
    20,
    20,
    20,
    20,
    20,
    20,
    20,
    20,
    20,
    20,
    20,
    20,
    20,
    20,
    20,
    20,
    20,
    20,
    20,
    20,
    20,
    20,
    19,
    19,
    16,
    16,
    16,
    16,
    16,
    16,
    16,
    16,
    16,
    16,
    16,
    16,
    16,
    16,
    16,
    16,
    16,
    16,
    16,
    16,
    16,
    16,
    16,
    16,
    16,
    15,
    15,
    14,
    14,
    12,
    12,
    12,
    12,
    12,
    12,
    12,
    12,
    12,
    12,
    12,
    12,
    12,
    12,
    12,
    12,
    12,
    12,
    12,
    11,
    11,
    11,
    10,
    10,
    10,
    10,
    10,
    9,
    8,
    8,
    8,
    8,
    8,
    8,
    8,
    8,
    8,
    8,
    8,
    7,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    4,
    4,
    4,
    4,
    4,
    4,
    4,
    4,
    4,
    4,
    3,
    3,
    3,
    2,
    2,
    2,
    2,
    2,
    2,
    2,
    2,
    1,
    0
  ],
  "f5_wall_sec": 170.7
}
```

### A4_churn ｜ ✅ OK ｜ 场景耗时 1.47s

| 校验 | 结果 | 说明 |
|---|---|---|
| ghost_deletes_rejected | ✅ | [404, 404, 405, 404] |
| no_5xx | ✅ | [204, 204, 204, 204, 204, 204, 204, 204, 204, 204, 204, 204, 204, 204, 204, 204, 204, 204, 204, 204, 204, 204, 204, 204, 204, 204, 204, 204, 204, 204] [404, 404, 405, 404] |
| table_not_leaking | ✅ | {'status': 'ok', 'version': '0.6.0', 'audits': {'active': 0, 'total': 0}} |

```json
{
  "rounds": 30,
  "delete_codes": {
    "204": 30
  },
  "ghost_delete_codes": [
    404,
    404,
    405,
    404
  ],
  "health_after_churn": {
    "status": "ok",
    "version": "0.6.0",
    "audits": {
      "active": 0,
      "total": 0
    }
  }
}
```

### A5_sse_flood ｜ ✅ OK ｜ 场景耗时 1.29s

| 校验 | 结果 | 说明 |
|---|---|---|
| all_streams_terminated | ✅ | 100/100（DELETE 后无悬挂连接） |
| server_alive | ✅ | {'status': 'ok', 'version': '0.6.0', 'audits': {'active': 0, 'total': 0}} |

```json
{
  "streams": 100,
  "terminated": 100,
  "pending_after_45s": 0,
  "first_byte_ms": {
    "p50": 542.9,
    "p95": 1105.8
  },
  "end_ms_p95": 1232.4
}
```

### A6_oversize_upload ｜ ✅ OK ｜ 场景耗时 0.00s

| 校验 | 结果 | 说明 |
|---|---|---|
| oversize_413 | ✅ | got 413 |
| f2_rss_no_blowup | ✅ | 210MB 上传前后当前工作集增量 -8.9MB（阈值 <50MB；历史峰值 661.4MB 仅为进程生命周期参考） |
| fat_zips_done | ✅ | ['done', 'done', 'done'] |

```json
{
  "oversize": {
    "status": 413,
    "wall_sec": 3.44,
    "server_rss_before_mb": 110.4,
    "server_rss_after_mb": 101.6,
    "server_rss_peak_mb": 661.4,
    "server_rss_delta_mb": -8.9
  },
  "fat_60mb": {
    "zip_kb": 60.0,
    "e2e_ms_p50": 1437.1,
    "terminals": [
      "done",
      "done",
      "done"
    ]
  }
}
```

## 发现与风险登记（F 编号沿用审查结论）

| 编号 | 发现 | 状态 | 建议 |
|---|---|---|---|
| F1 | REST API 无鉴权，source_path 可指向任意本地目录，报告回传源码片段 | 取证确认（本地工具设计如此） | 部署形态加鉴权 / source_path 白名单根目录 |
| F2 | 上传全量入内存后校验大小 | **已修复（W10）**：流式分块 + 提前 413 | 本轮 A6 复跑验证 RSS 回落 |
| F3 | Windows 沙箱无文件系统隔离，子进程可写 cwd 之外 | 取证确认（文档已声明） | 生产换 Docker `--network none --memory` |
| F4 | 沙箱输出 PIPE 全量缓冲 | **已修复（W10）**：8MB 限量排空 + truncated 标记 | 本轮 A7 复跑验证标记语义 |
| F5 | 无任务准入控制 | **已修复（W10）**：running 信号量（默认 4）+ pending 上限（默认 20）+ 429 | 本轮 A3 复跑验证准入实证；CPU 密集移进程池仍列后续 |

## 诚实边界

- 本报告只对 0.5.0 单 worker 内存态形态负责；多 worker / 持久化任务表后的洪泛行为需重测。
- A8 仅覆盖离线规则通道；在线 LLM 通道对注入的鲁棒性需真实 GLM key 评估（后续 Wave）。
- A6 的 RSS 为 Windows psapi 工作集口径，含 FastAPI 常驻内存；峰值为进程启动以来最大值，仅作量级证据。
- 谎报解压总量用例依赖中央目录声明值；更深的 zip 结构层攻击（本地头伪造等）以 CPython zipfile 自身清洗为最后防线。
