# L2 金丝雀双版本回放报告（bench/canary/replay.py，W10-A3）

## 环境

- **生成时间**：2026-09-15 12:58:04
- **旧版本**：git ref `v0.5.0`（git worktree 检出至临时目录，端口 8915，import audit 走旧代码）
- **新版**：git ref `HEAD`（项目根工作区实际状态运行，含未提交改动，端口 8916）
- **模式**：完整回放（读端点 + 上传审计全链路）
- **离线保证**：启动前清除环境变量 GLM_API_KEY / GLM_BASE_URL / GLM_MODEL（本次实际清除：无，环境本就未设置）
- **依赖**：双端共用当前 Python 环境（不做 pip install）
- **归一化口径**：JSON 递归剥离 `audit_id` / `created_at` / `duration_sec` / `schema_version`（health 另剥离 `version`）；任务 ID 作为**值**出现时（上传件被存为 `<audit_id>.zip`，报告 `project_name` 即该文件名 stem）统一替换为 `<audit_id>` 占位符；md/html 按行剔除含「审计 ID / 生成时间戳 / 总耗时」的行——两端任务 ID（新建即生成）与时间戳/耗时必然不同，属预期差异，不计入分歧

## 逐请求对照

| # | 请求 | 旧版状态 | 新版状态 | 结果 |
|---|---|---|---|---|
| 1 | `GET /api/health` | 200 | 200 | PASS |
| 2 | `GET /api/audits（空表）` | 200 | 200 | PASS |
| 3 | `POST /api/audits/upload（mini_app zip）→ 轮询至终态（≤240s）` | 200/done | 200/done | PASS |
| 4 | `GET /api/audits/{id}/report?format=json` | 200 | 200 | PASS |
| 5 | `GET /api/audits/{id}/report?format=md` | 200 | 200 | PASS |
| 6 | `GET /api/audits/{id}/report?format=html` | 200 | 200 | PASS |
| 7 | `GET /api/audits/{id}/issues?limit=1000` | 200 | 200 | PASS |

## 差异明细

（无差异：全部请求归一化后一致）
## 结论

**PASS** —— 7/7 项请求归一化后一致

## 诚实边界

- 新版服务以项目根**工作区实际状态**运行（含未提交改动），`--new-ref` 仅作报告标注；
- 任务 ID / 生成时间 / 耗时为运行期必然不同的字段，已由归一化剥离；
- 若规则库在双版本间变更，mini_app 的 issues/report diff 会非空——这正是金丝雀要抓的语义漂移，本报告如实呈现；是否属预期契约演进需人工判读差异明细，工具不做定性；
- quick 模式只回放 health + 任务列表两项，供冒烟，不等价于完整语义对拍。
