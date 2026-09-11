## 改动说明

<!-- 做了什么、为什么做；关联 issue 请写 Closes / Fixes #编号 -->

## 变更类型

- [ ] feat 新功能
- [ ] fix 缺陷修复
- [ ] docs 文档
- [ ] refactor 重构
- [ ] test 测试
- [ ] chore / ci 其他

## 影响范围

<!-- 列出本次触碰的目录 / 文件；并行开发请确认未越出本任务的独占目录（见 docs/06~09 协作规约） -->

- [ ] 未修改契约文件与禁改区（契约 v1.4 字段、`.github/workflows/ci.yml`、tests 既有用例、audit 公共模型等）
- [ ] 未改变既有进度事件文案关键字（Web 端阶段进度判定依赖）

## 自查清单

- [ ] 全量测试零回归：`python -m pytest tests -q` 通过
- [ ] 静态检查零告警：`ruff check .` 通过
- [ ] 新增 / 变更行为附带单测（如适用）
- [ ] 文档同步更新（README / docs-site/ / CHANGELOG，如适用）
- [ ] 触碰流水线时离线演示可跑通：`python demo/run_demo.py` 退出码 0
