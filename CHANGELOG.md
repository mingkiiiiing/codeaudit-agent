# 更新日志

本项目所有显著变更记录在此文件。

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [未发布]

Wave 9 + Wave 10 + Wave 11：测试体系补全（W9）、服务治理与灰度基建（W10）、形态演进（W11）。契约 v2.1/v2.2 微增，全部向后兼容；W9 见 [docs/14](docs/14-Wave9总体方案-测试体系补全与灰度发布.md)，W10 见 [docs/15](docs/15-Wave10总体方案-服务治理与灰度基建.md)，W11 见 [docs/16](docs/16-Wave11总体方案-持久化与多worker形态演进.md)。

### Added（Wave 11）

- **任务持久化（契约 v2.2）**：新模块 `audit/taskstore.py`——SQLite WAL 任务存储（标准库零依赖，线程锁串行化写），任务、事件流、报告全部落库（`<work_root>/audits.db`，`CODEAUDIT_DB_PATH` 可覆盖）；**服务重启后终态任务与报告仍可查询下载**（新能力），遗留非终态启动时 sweep 为 failed（`服务重启中断`）；FIFO 容量淘汰迁移到 store。启动 sweep 经 FastAPI lifespan 执行（导入零副作用）。
- **线程池执行**：审计任务从共享事件循环迁到独立线程（`asyncio.to_thread`，每任务独立事件循环）——根治 CPU 密集段饿死服务循环的问题（W9 观察到的 POST 响应推迟、health 失联不复现），读端点在重审计负载下全程可响应。
- **协作式取消（语义诚实声明）**：DELETE 不再瞬时打断线程，改为事件边界取消——执行协程每次 emit 前检查取消标志/表项存在性（表项被删即取消），七阶段均频繁 emit，典型亚秒级生效；DELETE 的 HTTP 语义不变（204 + 全端点 404 + SSE 收流）。
- **多 worker（实验特性）**：`codeaudit serve --workers N`（默认 1）——N>1 以 import string 形式启动 uvicorn 多进程，sticky 执行模型（任务由接收 worker 执行），SQLite 跨 worker 可见性经双 worker 冒烟实证（跨进程建/查/删/报告全通）；429 准入计数升级为全局口径（count_active 走 store），并发上限 = workers × 每 worker 上限。
- **内存归因诊断工具**（`bench/memdiag/run_memdiag.py`）：逐任务 tracemalloc 快照 diff + RSS 采样——**结论：库层（纯规则）RSS 稳态斜率 ≈ 0**，soak 观察的每任务缓爬不在 audit/* 复现（归因服务端簿记层，W11 持久化改造后由 soak 复跑复核）；报告见 [bench/results/memdiag_w11.md](bench/results/memdiag_w11.md)。

### Changed（Wave 11）

- SSE 事件流改 store seq 游标读取（回放/跟随语义不变）；既有 server/integration 用例随持久化迁移适配（断言语义未放宽，适配清单见 docs/16 收口记录）；`bench/stress` 场景 5 清理逻辑随形态更新（db 随场景临时目录销毁）。

### Added（Wave 10）

- **任务准入控制（F5 清偿，契约 v2.1）**：全局信号量限制同时运行的任务数（`CODEAUDIT_MAX_RUNNING`，默认 4），超出的任务停留在既有 queued 状态排队执行；非终态任务总数达上限（`CODEAUDIT_MAX_PENDING`，默认 20）时 `POST /api/audits` 与 `/api/audits/upload` 返回 **429**（`服务器并发审计数已达上限（N），请稍后重试`）——契约 v2 其余响应码不变。排队中任务 DELETE 语义不变（取消 + 「任务已取消」）。
- **金丝雀双版本回放工具**（`bench/canary/replay.py`，`make canary`）：git worktree 检出旧 tag 起双端口服务，固定请求集（health / 列表 / mini_app 上传审计至 done / 报告三格式 / issues）双发对拍，归一化（键级剥离 audit_id/created_at/duration_sec/schema_version + 值级替换任务 ID）后 diff——diff 非空即语义漂移，阻断发布（退出码 1）。自验：v0.5.0 vs HEAD 7/7 项一致 PASS。
- **Soak 持续混合负载压测**（`bench/stress/run_soak.py`，`make soak`）：2 Hz 读负载 + 8s 周期双素材任务提交 + 30s SSE 拍点 + 10s RSS/health 采样；判定四准则（无 5xx / 接纳任务终态率 100% / RSS 稳态斜率 < 1 MB/min / 结束 total ≤ 50）；报告内置 RSS 诊断节（全窗口 vs 稳态窗口斜率、RSS-任务数相关性）。
- **沙箱输出限量 + truncated 标记（F4 清偿）**：`_drain` 改为 8MB 限量排空（超限后继续读但丢弃，杜绝子进程 PIPE 写阻塞误杀），`SandboxResult` 新增 `truncated` 字段，被限量的 tail 首行标注 `[output truncated]`；`run()/run_tests()` 签名与既有语义不变。
- 文档站 nav 补登记 Wave 8/9/10 三份方案文档（mkdocs strict 构建通过）。

### Changed（Wave 10）

- **流式上传（F2 清偿）**：`POST /api/audits/upload` 改为 256KB 分块落盘（`.part` 临时文件 + `os.replace` 原子提交），累计超 200MB 立即 413 并停止读取——不再全量缓冲进内存（W9 对抗基线 210MB 上传 RSS 峰值 660MB，复跑验证回落，见 bench/results/adversarial_w10.md）；400/413/取消路径临时文件零残留，落盘字节逐一致语义不变。
- adversarial runner 适配契约 v2.1：A3 场景 F5 段升级为准入实证（25 路并发 > pending 上限，校验无 5xx / active 峰值 ≤ 20 / 被接纳者全终态）、A6 场景新增 RSS 回落校验（>400MB 判失败）、A7 场景适配 truncated 标记语义；风险登记表 F2/F4/F5 状态更新为已修复。
- soak RSS 判定口径修订（W10-A5 集成裁决）：剔除前 60s 预热样本（冷启动懒加载导入属一次性抬升非泄漏），稳态样本不足回落全窗口；全窗口斜率与 RSS-任务数相关性继续如实报告，每任务 ~0.15MB 缓爬的长窗口归因列 W11 待办。

### Added（Wave 9）

- **对抗与滥用测试运行器**（`bench/adversarial/run_adversarial.py`，`make adversarial`）：八场景真实 HTTP + 库层双口径——恶意 zip 军火库（路径逃逸 / 1GB 声明量炸弹 / 坏 zip / 空 zip / 奇葩文件名 / 40 层深嵌套 / 二进制与 3MB 单行 .py，含 marker 全树逃逸扫描）、参数与路径滥用矩阵（20 探针精确 4xx）、任务表洪泛（60 任务 FIFO 淘汰 + 并发准入实证）、create→DELETE 抖动竞态、100 路 SSE 中途 DELETE 收流、210MB 不可压缩上传（413 + 服务端 RSS 取证）、沙箱逃逸四件套（超时击杀 / 200MB 输出炸弹 / 白名单拒绝 / 越出 cwd 写文件取证）、提示注入离线实证（注入 payload 不影响规则通道对真缺陷的判定）。报告见 [bench/results/adversarial_w9.md](bench/results/adversarial_w9.md) 与复跑 [adversarial_w10.md](bench/results/adversarial_w10.md)。
- **算法不变量测试**（`tests/property/test_algorithm_invariants.py`，进 CI）：审计确定性（同输入两次问题清单逐字段一致）、健康分单调性（追加 critical 不升分，50 组随机性质 + 值域边界）、干净语料零 critical/high 误报、SARIF 2.1.0 结构不变量、summary/issues 计数自洽、过滤与分页端到端不变量（severity 归一化语义、offset 游走拼回无重无漏）。
- **灰度发布保障测试**（`tests/integration/test_gray_release.py`，进 CI）：旧版报告（v0.3 形态，缺 v1.7 字段）在当前代码可反序列化 + md/html/SARIF 三格式渲染 + 往返不漂移；特性开关 A/B 等价性（rule_scan_workers 串行 vs 并行、ingest 硬链接 vs 复制，语义快照相等）；API 路由面冻结（openapi paths 与契约 v2 清单精确相等，多删都红）。
- **审查发现登记 F1–F5**（docs/14 §1）：无鉴权任意路径审计（取证：仓库外目录可审计且报告回传源码片段）、上传全量缓冲、Windows 沙箱无文件系统隔离、沙箱输出无界缓冲、任务无准入控制（F2/F4/F5 已于 W10 清偿，F1/F3 属本地工具设计边界继续登记）。

## [0.5.0] - 2026-09-13

Wave 8：Web 前后端。配套前端从单文件演示页升级为工程化 React 工作台（`frontend/`，React 18 + TypeScript + Vite + Ant Design 5 + ECharts），REST API 扩展为契约 v2（任务列表 / 删除 / zip 上传 / 重构方案 / 架构理解 / 健康检查），`python cli.py serve` 单进程同时服务 API 与前端构建产物。技术选型经 GitHub 开源调研后锁定（antd 99.5k★ / ECharts 67.3k★ / DefectDojo 交互范式参考），方案与任务分解见 [docs/13](docs/13-Wave8总体方案-Web前后端.md)。

### Added

- **前端工作台 `frontend/`**（W8-A2 / W8-A3）：三页式 SPA——「仪表盘」（任务列表：状态标签、创建时间、查看 / 删除，非终态任务 5s 自动刷新）、「新建审计」（服务端本地路径 / zip 拖拽上传双入口 + 修复与单测开关）、「任务详情」（运行中：七阶段 Steps 进度 + SSE 实时事件日志（断流自动降级轮询）；完成后：健康分 gauge 与严重度分布（ECharts）、问题列表（severity / category 服务端过滤 + 关键词过滤 + 展开详情与代码片段）、修复补丁 diff 视图、重构方案卡片、架构理解通用渲染、报告三格式下载）。中文界面，antd zhCN，类型化 API 客户端与后端契约逐字对齐。
- **REST API 契约 v2**（W8-A1）：`GET /api/health`、`GET /api/audits`（分页任务列表，新→旧）、`DELETE /api/audits/{id}`（终态移除 / 运行中先取消）、`POST /api/audits/upload`（multipart zip，魔数与 200MB 上限校验，413 / 400 明确报错）、`GET /api/audits/{id}/refactors`（重构方案，暴露 0.4.0 已有但未上 API 的 RefactorProposal）、`GET /api/audits/{id}/understand`（架构卡片）。SPA 静态托管：`frontend/dist` 存在时根路径与客户端路由兜底到 SPA，否则回退单文件演示页（保留为零构建入口）；开发态 CORS 精确放行 Vite 5173 端口。
- **Web 压测基线**（`bench/stress/run_stress_web.py`）：真实 HTTP 口径四场景——读端点突发（32 并发 × 560 请求）、并发审计（6 任务同时提交到终态）、SSE 并发流（24 连接依赖历史回放）、zip 上传（12 次 + 非 zip 负样本必须 400）；数据见 [bench/results/stress_web_w8.md](bench/results/stress_web_w8.md)。
- **CI 前端 job**：typecheck（tsc --noEmit）+ vitest + vite build 三关，与 Python 三矩阵并行。

### Changed

- 任务表条目新增 `created_at / source_path / do_fix / do_tests`（列表与详情端点可见）；依赖新增 `python-multipart`（multipart 上传运行时必需）。
- 任务列表 / 删除为**内存态**（进程重启即清空），如实标注，持久化留作后续版本。

## [0.4.0] - 2026-09-12

Wave 7：赛题合规收口与算法提速。补齐赛题要求的最后一块功能拼图（自动生成重构方案），打通 JS/TS 修复与单测验证闭环，并行/硬链接提速以可选开关落地并附诚实压测对比。合规矩阵 10 项对照的最终状态见 [docs/12 §7](docs/12-Wave7总体方案-赛题合规与提速.md)。

### Added

- **重构方案生成器**（`audit/refactor`，W7-A2）：确定性启发式 + LLM 增强双层——确定性层零 LLM 可出（长函数 top-N 分解、重复代码聚类、热点模块拆分、循环依赖提示，全部从已有索引与规则命中聚合），LLM 层深化每条方案的 rationale/steps（未配置 Key 时安全降级为纯启发式）；审计报告新增**「重构方案」章节**（md / html / json 三格式同步，SARIF 不含重构项），报告契约 v1.7 微增 `RefactorProposal`（id/title/target/kind/rationale/steps/related_issues/source）。
- **JS/TS 修复与单测验证闭环**（`audit/fix` / `audit/testgen`，W7-A3）：JavaScript / TypeScript 问题同样进入修复闭环——语法验证走 tree-sitter 重解析 + `node --check`（探测可用才启用），单测验证走 `node --test`（node 18+ 内置，探测可用才执行，不可用时语法验证结果诚实标注），沙箱白名单相应扩展；CI 的 ubuntu / windows runner 自带 node 实证。
- **压测对比数据**：W7 vs W6 同负载对拍（串行 vs 并行规则扫描、硬链接 vs 复制物化、命中集合一致性校验），数据见 [bench/results/stress_w7_clean.md](bench/results/stress_w7_clean.md)。

### Changed

- **性能实验与开关化**（W7-A1）：规则扫描多进程并行（`rule_scan_workers`，`0` 自动 / `1` 串行默认 / `>1` 指定并发，文件数 ≥100 才启用进程池）与 ingest 同盘硬链接物化（`link_same_volume`，**默认关闭**走整树复制，跨盘 / 失败自动回退）均作为**可选配置**交付。本机 2000 文件档实测并行仅 -7.1%、硬链接路径相对复制 +70.3%，负收益已诚实回退默认值（规则并行默认串行、ingest 默认复制），设计保留待后续复跑；行为等价有保障——并行与串行的规则命中集合指纹一致（41 = 41）。

## [0.3.0] - 2026-09-12

Wave 5 + Wave 6：质量攻坚与健壮性清偿、规则库扩充、依赖约束治理。Wave 5 三路只读审查 + Dogfood 自审计共产出 68 项发现，本版修复其中 25 项核心问题，并新增联调测试套件（34 用例）与压力测试基线（2000 文件档纯规则审计 0.245 s/KLOC）；Wave 6 完成 R4 复核清偿（含 4 项必修缺陷）、静态规则库 49 → 63 条并上线内置规则手册、依赖约束治理。

### Added

- **规则库扩充至 63 条**：静态规则库 49 → 63 条（Python 35 / JavaScript 21 / TypeScript 专属 7），新增 14 条规则（Python 8、JavaScript 与 TypeScript 6），每条均附正反例测试；新增**内置规则手册** [docs-site/rules.md](docs-site/rules.md)——由 `python scripts/gen_rule_docs.py` 从规则注册表自动生成，含全部规则的判定说明与统计，规则与文档不再漂移。

### Fixed

- **密钥规则复数形态漏报（上版回归）**：0.2.1 引入的标识符分词精确匹配未覆盖复数形态，`API_KEYS` / `dbPasswords` / `credentials` 等命名全部漏报；token 归一化补齐复数形式后重新命中，并附复数形态正反例回归测试。
- **增量索引陈旧缓存**：索引预取缓存在同进程多次构建间未重置，改动 import 后的增量构建仍按陈旧依赖解析符号；构建开始时统一清空缓存，并补「改 imports 二次构建」回归用例。
- **删除 / 重命名补丁回滚**：补丁备份与生效性校验此前只覆盖 `+++ b/` 侧路径，删除型与重命名型补丁回滚不完整；现按 diff 两侧路径并集处理，覆盖全部补丁形态。
- **LLM 审查生效修复（critical）**：simple 审查模式下回调参数错绑，导致 LLM 通道完全不参与审查、报告却按「规则 + LLM」口径呈现；修复后 simple 模式真实生效。
- **修复 Patch 静默跳过**：嵌套 git 仓库场景下 `git apply` 可能在用户原始项目的外层仓库执行而被静默跳过；补丁的应用与回滚现严格限定在审计工作副本内。
- **SARIF 文件 uri 跨平台**：Windows 上生成的 SARIF `uri` 含反斜杠、不符合规范；统一归一为 `/` 分隔，各平台均可通过 upload-sarif 校验。
- **密钥规则误报**：PY-HARDCODED-SECRET 规则改为标识符分词精确匹配敏感词，并叠加熵 / 字符集随机性校验，普通命名常量（如内部指纹键名）不再被判为 critical。
- **服务端报告目录按任务隔离**：报告改写入 `<work-root>/<audit_id>/reports/`，并发或连续多次审计不再相互覆盖。

### Changed

- **understand 阶段随 ingest 门控**：ingest 失败时理解阶段与报告统计一并跳过，不再回读用户原始目录。
- **任务取消终态落盘**：客户端断开或任务取消（CancelledError）时任务以终态落盘并退出运行队列，不再悬挂为 running。
- **依赖约束治理**：完成第三方依赖与 CI Action 版本约束的收敛治理，降低供应链与构建漂移风险。
- **接入失败门控**：ingest 失败时 index / detect / fix / testgen 全部跳过并发「跳过：…（ingest 未成功）」事件，报告为空壳——绝不触碰用户原始目录、绝不向原项目写入补丁。
- **资源收口**：审计结束统一关闭 LLM 客户端与索引连接，长跑与批量场景不再泄漏连接。
- **服务端任务表有界化**：终态（已完成 / 失败）任务记录按 LRU 淘汰，服务长期运行内存不再无限增长。
- **门禁消息走 stderr**：`[门禁] 通过 / 未通过` 判定信息输出到 stderr，`--json --check` 组合下 stdout 为纯 JSON，可整体 `json.loads`。
- **`.codeaudit/` 默认忽略**：审计工作区目录加入流水线默认忽略清单（与 `.git`、`node_modules` 并列），对已含工作区的项目复跑不再产生嵌套副本与假问题，无需手动配置 gitignore。
- **程序名动态显示**：帮助 / usage 中的程序名按调用方式显示——`python cli.py` 显示 `cli.py`，安装态显示 `codeaudit`。
- **文档站与协作设施完善**：文档站导航收录设计文档 00、首页链接口径对齐；CLI 文档补全 `--fail-on` 单独指定即隐含启用门禁、程序名与门禁消息流向说明；README 区分离线基线（`bench.run`，零 Key 可跑）与真跑评估（`bench.real_run`，需 API Key）、注明 `.env` 不自动加载需手动 export、目录树补全 `scripts/` 与压测目录、Makefile 清单补 `clean`；离线 run 记录头部加注消融表配置数口径；CI / 压测调试残留路径统一纳入 `.gitignore` 与 ruff 排除。

### Security

- **超长行搜索降级**：除 zip 接入防护外，沙箱内的文件搜索对超长行自动降级处理，恶意构造的超长行不再拖垮审查进程。
- **zip 炸弹防护**：zip 接入增加 1 GB 累计解压上限，超限中止并明确报错，恶意超大压缩包不再拖垮进程。
- **测试目标注入防护**：run_tests 工具拒绝以 `-` 开头或越界的测试目标，阻断参数注入路径。

## [0.2.0] - 2026-09-11

Wave 4：开源生态对标（SARIF / CI 门禁 / 增量审计 / 配置文件 / 打包入口 / 协作与文档设施）。

### Added

- **SARIF 输出**：`--format sarif` 生成 SARIF 2.1.0 报告 `report.sarif`，配合 `github/codeql-action/upload-sarif@v3` 可上传 GitHub Security tab（公共仓库免费）；报告契约新增 `schema_version` 字段。
- **CI 门禁**：`--check --fail-on <sev>`（`critical|high|medium|low`），命中阈值时退出码 `3`；退出码约定固化为 `0` 完成 / `1` 运行错误 / `2` bench 缺 key / `3` 门禁失败。
- **增量审计**：`--diff <git-ref>` 只审相对 ref 发生变更的文件，面向 PR 场景。
- **基线抑制**：`--report-baseline` 生成存量问题基线（问题指纹 sha1），`--baseline` 加载基线抑制命中项并计入 `stats.suppressed`；行内忽略 `# codeaudit: ignore[规则ID]`。
- **配置文件**：支持 `.codeaudit.toml` 与 `pyproject.toml [tool.codeaudit]` 自动发现，优先级 CLI > 环境变量 > 配置文件 > 默认值，未知键 / 非法值输出警告（`config_warnings`）。
- **入口点**：`pip install -e .` 后提供 `codeaudit` 命令，版本号单源（`pyproject.toml` 与 `audit.__version__` 一致）。
- **GitHub 协作设施**：issue / PR 模板、CODEOWNERS、Dependabot（pip 与 github-actions 每周巡检）、`release.yml`（tag 触发自动建 Release）与 `docs.yml`（文档站自动发布 gh-pages）。
- **文档站**：mkdocs-material 站点（`docs-site/`），含 CLI 全参数速查、SARIF 上传实践、PR 增量审计实践与 Roadmap；设计文档 01~09 由构建期镜像收录。

### Changed

- README 收口：新增「数据隐私」「CI 门禁与增量审计」「配置文件」「Roadmap」节，徽章区补 Release / Docs，安装节补 `codeaudit` 入口用法。

## [0.1.0] - 2026-09-11

Wave 1~3：核心流水线、检测与修复能力、产品化入口与评估基建。

### Added

- **七阶段流水线**：Ingest（路径 / zip、语言识别）→ Index（tree-sitter 符号表 + 调用图，SQLite）→ Understand（map-reduce 架构卡片）→ Detect（双通道）→ Fix（Patch 生成 + 沙箱验证）→ TestGen（pytest 生成 + 沙箱运行 + 失败重试）→ Report。
- **双通道检测**：tree-sitter AST + 正则静态规则库 49 条（覆盖 bug / performance / security / style，支持 Python / JavaScript / TypeScript）负责高召回；LLM Review Agent（上下文注入、`find_references` 等工具取证）+ Verify Agent 复核负责高精确；未配置 `GLM_API_KEY` 自动降级纯规则离线模式。
- **修复与单测闭环**：critical / high 生成 unified diff，经 `git apply --check`、语法重解析、运行项目现有测试三重验证，通过才标记 `verified`；对目标函数生成回归单测并在沙箱运行，失败带 traceback 重试。
- **报告三格式**：JSON / Markdown / HTML + 健康分（加权问题密度）；单阶段失败只记入报告不中断审计。
- **三端入口**：CLI（`run / index / report / serve`）、REST API（异步任务 + SSE 进度 + 仪表盘）、单文件 Web 演示页。
- **评估基准**：240 条金标（10 个项目集）、匹配与消融脚本、效率对比；离线纯规则基线实测 Precision(critical+high) 1.000 / Recall 0.844 / P50 5.0 s/KLOC（见 `bench/results/`；LLM 通道指标待真跑）。
- **工程化**：650 项单元测试全绿（Wave 3 收口基线）、GitHub Actions CI（Python 3.11 / 3.13 × ubuntu / windows 矩阵）、离线全闭环演示 `python demo/run_demo.py`。

[Unreleased]: https://github.com/mingkiiiiing/codeaudit-agent/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/mingkiiiiing/codeaudit-agent/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/mingkiiiiing/codeaudit-agent/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/mingkiiiiing/codeaudit-agent/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/mingkiiiiing/codeaudit-agent/releases/tag/v0.1.0
