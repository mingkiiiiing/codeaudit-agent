# mini_app —— CodeAudit 演示靶项目

这是 CodeAudit Agent 的**演示靶项目**（配套 `demo/run_demo.py` 离线全闭环演示使用），
不是真实业务代码，请勿安装或部署。

内容说明：

- `store.py` 的 `find_user` 故意埋入一个 **SQL 字符串拼接注入缺陷**
  （静态规则 `PY-SQL-INJECTION`，severity=critical），是演示中"检测 → 修复"的靶点；
- `tests/test_store.py` 是项目**现有测试**，修复前后均应通过——
  修复阶段在沙箱中运行它来验证 Patch（全部通过才标记 `verified`）；
- `textutil.py` / `main.py` 保持干净，让演示输出聚焦在单一缺陷上。

其他入口：

- `python demo/run_demo.py`：离线全闭环演示（推荐）；
- `python cli.py run demo/mini_app --no-llm`：只看纯规则检测（离线，无修复/单测）；
- 配置 `GLM_API_KEY` 后 `python cli.py run demo/mini_app --fix --tests`：真实 LLM 全流程。
