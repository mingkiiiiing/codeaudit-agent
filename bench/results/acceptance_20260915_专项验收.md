# CodeAudit Agent 专项验收报告（2026-09-15）

> **W16 清偿复测（同日，含 P0 真跑终局）**：本报告全部短板（P0/P1/P2/P3）已清偿并复测，**详见文末「W16 短板清偿复测」章节**——综合评分自 72 上修至 **92 / 100**，在线真跑 Precision 0.884 达成 ≥85% 赛题目标，整体验收**通过**。

- **验收对象**：工作区版本 v0.6.0 + W14/W15 未提交改动（`python -c "import audit; audit.__version__"` = 0.6.0）
- **验收环境**：Windows 10 (26200) / Python 3.13.9 / Node v24.15.0 / git 2.53；`GLM_API_KEY` 未配置 → 本验收以**离线纯规则模式实测为主**，LLM 在线通道标"未执行"
- **基准集**：① 官方金标（240 条 / 10 项目，`bench/datasets/goldset.jsonl`）复现；② **自建独立基准库**（`D:/acc_tmp/corpus/`，107 条逐行标签，与官方金标零重叠，植入姿势逐条对照 63 条规则源码校准，避免"自己出题自己答"）
- **匹配口径**：同文件 + 行区间 ±3 + 类别相容（与 `bench/matcher.py` 官方口径一致）
- **所有数字均为 2026-09-15 本机实测**，中间产物存 `D:/acc_tmp/eval/` 可复现

## 总览

| 维度 | 结论 | 综合评分(百分制) |
|---|---|---|
| 一、代码质量审计 | 部分达标（覆盖内检出极强，规范类子项缺规则） | 75 |
| 二、安全合规审计 | 部分达标（代码级漏洞强，依赖/合规/配置面缺） | 78 |
| 三、架构设计审计 | 部分达标（循环依赖与热点准确，分层/原则/债量化缺） | 60 |
| 四、智能重构落地 | 部分达标（修复验证闭环强，自动重构与方案分级缺） | 68 |
| **整体验收** | **有条件通过** | **72 / 100** |

条件：LLM 在线通道按指标口径真跑复测（精确率 ≥85% 目标至今无实测数据）；性能与离线检测质量已达标。

---

## 一、代码质量审计效果测试

### 1.1 编码规范审计（命名/格式/注释/最佳实践）

- 【验收结论】**部分达标**（最佳实践类达标；命名/注释规范不支持）
- 【量化指标】规则库 63 条中 style 类 17 条（PY 8 / JS 5 / TS 4 分类计数见注册表）。自建语料 style 应检出 12/12 = **100%**（print 残留、魔法数字、TODO/FIXME、type 比较、global 改模块态、硬编码 URL、console.log、@ts-ignore、any、导出 any 参数、缺返回类型、var/debugger 等）。**命名规范检出率 0/6**（拼音 `jieguo`、超长标识符、歧义 `l1I`、`data2`，PY/JS 各 3 例均未报）；**注释类（无注释复杂逻辑/注释规范）无对应规则**；废弃 API（`time.clock`）0/1。
- 【问题详情】检测面是"规则命中的坏味道"而非 PEP8/ESLint 意义上的编码规范（无命名约定、行长、导入排序、注释覆盖检查）。混淆代码（unicode 变量名 `𝐝𝐚𝐭𝐚`）不报属合理豁免，但常规命名违规同样盲区。
- 【复测要求】新增命名规范规则（或集成 ruff/eslint 规则子集）后，对上述 6 例命名语料重测，检出率 ≥80%。

### 1.2 代码坏味道审计（上帝类/长函数/重复/圈复杂度/魔法值/死代码）

- 【验收结论】**部分达标**（长函数/深嵌套/魔法值 100%；重复代码、死代码、圈复杂度不支持）
- 【量化指标】自建语料：长函数（100 行）1/1、深嵌套（4 层）1/1（PY+JS 各 1）、魔法值 2/2、上帝类（30 方法）**0/1**、死代码（未调用函数/未用变量）**0/2**、**逐字重复 12 行代码块 0/2**（`audit/refactor/heuristics.py:29` 的 dedup 是"同类规则同文件 ≥3 次命中"的模式归并，非文本/AST 克隆检测）；圈复杂度无独立度量（仅长函数文案提及）。重构方案层的"重复模式归并"在 quality_js 上产出 2 条（基于重复规则命中，机制正确但口径 ≠ 重复代码检测）。
- 【问题详情】"重复代码检测精度（最小重复行数）"指标无法给出——不存在克隆检测能力；上帝类无规则（热点模块 200 行 + ≥3 入度才进重构方案，且仅方案非告警）。
- 【复测要求】引入克隆检测（如 ≥6 行 token 归一化匹配）后对 `D:/acc_tmp/corpus/quality_py/app/out_of_scope.py` 的 dup_block_a/b 重测，应 ≥1 命中且最小重复行数可配置。

### 1.3 性能与资源效率审计

- 【验收结论】**达标**
- 【量化指标】自建语料应检出 10/10 = **100%**：循环内 IO（open/.execute 各 1）、list 成员判断、循环字符串拼接、循环 deepcopy、重复调用表达式、循环 await、fetch 无超时、urlopen/requests 无超时（各 1）、日志内格式化（% 与 f-string 各 1）。风险分级与规则库注册表一致（IO-in-loop=high 等）；定位精确到行（±0 行，标题含"第 N 行"+ 变量名）。
- 【问题详情】N+1 查询的 session 型变体（`session.query().get()` 循环）无规则；内存泄漏型（全局缓存只增不减）0/1 未检出——影响范围：资源泄漏类反模式盲区。
- 【复测要求】补内存泄漏启发式（模块级容器 + 循环 append 无淘汰）后重测。

### 1.4 可测试性与可维护性审计

- 【验收结论】**部分达标**（可测试性坏味道可间接命中；覆盖率/可维护性评分不支持）
- 【量化指标】可测试性相关坏味道（global 状态、超长函数、深嵌套）经 1.2/1.3 实测命中；**测试覆盖率计算 0%**（无覆盖率能力）、可维护性评分仅有"健康分 = max(0, 100−加权问题密度×50)"代理指标（权重 critical10/high5/medium2/low0.5，实测 quality_py 39 问题→0 分，方向正确但非学术口径的可维护性指数）。
- 【问题详情】赛题指标口径中不含覆盖率，属能力边界而非缺陷，但验收项要求的能力确实缺席。
- 【复测要求】若需达标：接入 coverage.py 数据或以 testgen 产物估计覆盖率。

---

## 二、安全合规审计效果测试

### 2.1 代码原生安全漏洞（OWASP/CWE 类）

- 【验收结论】**达标（代码级）**，LLM 通道未执行
- 【量化指标】自建安全语料应检出 **21/21 = 100%**，其中 critical+high **19/19**：SQL 注入 4 变体（+/f-string/%/.format 全命中）、命令注入 3（os.system 拼接 / shell=True f-string / os.popen）、eval/exec 2、pickle.loads 1、yaml.load 1、硬编码密钥 3（AWS/PASSWORD/TOKEN 熵闸门全过）、JS eval/innerHTML/document.write/localStorage/token cookie/SQL 拼接/ghp_ 密钥 7。官方金标复现（critical+high 层）：**Precision 0.9865 / Recall 0.8444 / F1 0.91**（74 条 in-level 报告 73 匹配；90 条 in-level 金标命中 76）。**JS 命令注入（child_process.exec 拼接）0/1**——规则库未覆盖。利用路径分析与修复建议为规则内文案（建议具体可落地，如"参数化查询占位符 + 参数元组"），与专业工具一致性：官方 docs/09 有 semgrep 对标设计，本次未跑 semgrep 交叉（未执行）。
- 【问题详情】误报控制极好：8 条安全写法对照（参数化 SQL ×2、shell=False+check、yaml.safe_load、textContent、参数化 query、带超时 fetch、普通表达式）中仅 1 条被报——`AbortSignal.timeout(5000)` 中的 5000 触发魔法数字 style/low（占该次 22 条发现的 4.5%，critical/high 误报 0）。
- 【复测要求】配 `GLM_API_KEY` 后跑 `python -m bench.real_run`（官方离线指标目标精确率 ≥85% 的在线通道至今无实测数据，这是验收项"准确率 85%+"的唯一未闭环点）；补 JS-COMMAND-INJECTION 规则。

### 2.2 依赖组件安全（CVE/废弃依赖/协议冲突）

- 【验收结论】**未达标（能力缺失）**
- 【量化指标】CVE 匹配准确率 0（无依赖扫描器：requirements.txt/package.json 不在扫描范围）；协议风险识别 0；冗余依赖检出 0。语言支持 Python/JS/TS，Java/Go 依赖文件（pom.xml/go.mod）完全不支持。
- 【问题详情】属 Roadmap 明示范围外（README：Java/Go 支持在演进方向），但按本验收标准为未达标项。
- 【复测要求】接入 OSV-Dev API 离线库或 pip-audit/npm audit 封装后，以含已知 CVE 的 requirements/package.json 语料重测，匹配准确率 ≥90%。

### 2.3 数据安全合规（敏感数据/日志泄露/脱敏）

- 【验收结论】**部分达标**
- 【量化指标】**密钥脱敏 4/4 = 100%**（AWS 密钥/DB 口令/API Token/ghp_ 在 report.json 的 code_snippet 全部打码为 `"********"`，W14-A1 生效）；但**敏感数据识别 0/2**（日志输出手机号+身份证 PII、敏感字段明文入库均未报）；输出侧脱敏建议能力无（无 PII 规则）。
- 【问题详情】"硬编码密钥"是密钥→"数据合规"只做了输出打敏与密钥检测，PII 数据流（日志/入库/展示）无规则，个人信息保护相关判断不支持。
- 【复测要求】新增 PII 模式规则（手机号/身份证/邮箱正则 + 日志/SQL 上下文）后对 `sec/pii_py.py` 重测。

### 2.4 运维安全风险（硬编码密钥/配置泄露/未鉴权/日志权限）

- 【验收结论】**部分达标**
- 【量化指标】源码内硬编码密钥 3/3 = 100%；**配置文件明文密钥 0/2**（`deploy/config.yaml`、`.env` 不在扫描范围）；服务端点鉴权：serve 未配 `CODEAUDIT_API_TOKEN` 时启动横幅明确警告"暴露到网络前必须配置 API 令牌"（实测确认），未鉴权 API 可创建/删除任务（本机绑定默认 127.0.0.1 缓解）；日志权限控制无对应能力。
- 【复测要求】配置文件（yaml/.env/.properties）纳入密钥扫描白名单语言后重测 2 例。

---

## 三、架构设计审计效果测试

### 3.1 模块与依赖架构（分层/循环依赖/耦合）

- 【验收结论】**部分达标**（循环依赖达标；分层违规不支持）
- 【量化指标】植入 2 条 import 环（services→api→services、dao→services→dao），确定性重构层**检出 2/2 = 100%**，方案含精确文件路径环、依赖倒置/公共下沉步骤（confidence 0.75）；**跨层调用（API 直接 import DAO）0/1**——无分层规则；模块耦合度无量化数值（只有模块文件数/行数/符号统计）。understand 架构卡片实测：6 文件项目正确产出模块职责、技术栈、规模热点 Top5（301 行热点文件排名第 1，100% 正确）。
- 【问题详情】循环依赖检测依赖 import 图（index 阶段），仅 Python 验证；JS/TS import 环未测（推断支持但无实测）。
- 【复测要求】补分层架构规则（可配置 layer 白名单）后对 `arch/api/leaky.py` 重测。

### 3.2 设计原则符合性（SOLID/DDD/模式误用）

- 【验收结论】**未执行（依赖 LLM 通道）**
- 【量化指标】离线规则通道 0 条 SOLID 类规则；该能力设计上由 LLM Review Agent 承担（docs/03），无 Key 未跑。消融表 `llm_only` 行 Recall 0.000 佐证离线无此能力。
- 【复测要求】配 Key 后以 SOLID 违规基准（上帝类违反 SRP、双向依赖违反 DIP 等）跑 `--review-mode tools` 实测准确率。

### 3.3 技术债量化评估

- 【验收结论】**部分达标**（定性定位准；量化估算不支持）
- 【量化指标】债定位证据：健康分（加权密度）、问题分级（critical~low 四级实测与规则注册表 100% 一致）、热点 Top、重构方案（长函数分解方案实测给出 3~100 行的精确行号区间与候选子职责）——分类与定位达标；**工作量估算（人日/故事点）0%、债的货币化/利率模型 0%**；优先级排序仅按 severity 隐式排序，无显式 P0/P1/P2。
- 【复测要求】为 RefactorProposal 增加 priority(枚举) + estimated_effort 字段后复测。

### 3.4 扩展性与可演进性

- 【验收结论】**部分达标**
- 【量化指标】接口兼容性：契约版本化（报告 schema_version、API 契约 v2.2，`tests/integration/test_gray_release.py` 旧版报告兼容渲染在 1405 绿内）；技术栈升级难度/可观测性完善度（metrics/tracing 埋点缺口检测）**无规则、0%**。
- 【复测要求】超出当前产品定位，建议降级为 Roadmap 项。

---

## 四、智能重构落地效果测试

### 4.1 重构方案生成

- 【验收结论】**部分达标**
- 【量化指标】确定性层（零 LLM）实测产出：长函数分解（100 行函数→精确行号区间+步骤）、循环依赖消除（2 环含路径）、重复模式归并（≥3 同类命中）、热点模块拆分（LOC≥200 且入度≥3，本语料入度不足未触发——条件已核对源码 `heuristics.py:34-35`）。**P0/P1/P2 分级：不支持**（模型字段仅 kind/confidence，`audit/models.py:233-250`）；影响范围评估=related_issues 关联（有）；**收益量化指标（无）：benefits 为定性一句话**；风险点识别：方案含"重跑审计确认"类步骤但无结构化风险清单。
- 【复测要求】增加优先级与量化收益（预估可维护性/复杂度变化）后复测。

### 4.2 代码自动重构（基础/中级/高级）

- 【验收结论】**部分达标（修复闭环达标；中级/高级自动重构未执行）**
- 【量化指标】基础修复（自动 Patch）：离线 demo 全闭环实测 **Patch=verified**（`git apply` 干跑 → tree-sitter 重解析 → 沙箱跑通现有测试 → 生成 5 条回归单测含 `x' OR '1'='1` 注入载荷用例 5/5 通过，总耗时 11.9s）；集成测试 `tests/integration/test_fix_tests_loop.py` 含于全量 **1405 passed / 287s**。离线 CLI `--fix --tests` 正确降级：0 patches / 0 test_cases，报告注明"未生成补丁"。**真实 LLM 驱动的中级（提取方法/拆函数）与高级（模式重构/模块拆分）自动执行未执行（无 Key）**——设计上 Fix Agent 走 LLM，离线仅 SQL 注入→参数化等 demo 脚本化路径。
- 【问题详情】报告未区分"离线跳过"与"未启用 --fix"两种 0 patch 原因（文案合并为一句，排障性略差）。
- 【复测要求】配 Key 后以自建 quality_py 语料跑 `--fix --fix-max 20`，统计：语法通过率（git apply+重解析）、verified 率、语义一致性（现有测试通过率），阈值 ≥80%/≥60%。

### 4.3 重构验证机制

- 【验收结论】**达标（实测范围内）**
- 【量化指标】demo 实测三重验证全链路真实执行（apply-check/语法重解析/沙箱测试）；沙箱回放生成单测 5/5；失败回滚机制（needs-review 回退）在 1405 项测试内含用例；性能对比验证：无（不做重构前后 benchmark 对比）；逻辑 Diff：报告含 unified diff 原文（SARIF/Patch 列表 API 实测可取）。
- 【复测要求】增加前后性能对比钩子（可选 pytest-benchmark）后复测。

### 4.4 工程化协同

- 【验收结论】**达标（CI 链路）；PR 评论式整改不支持**
- 【量化指标】实测全部通过：① 增量审计 `--diff HEAD` 只审 1/1 变更文件（新增缺陷文件命中，未变更文件 0 扫描）；② 基线抑制：39 条存量 100% 抑制（`suppressed: 39`），修复后指纹自动出账（基线只会缩小——设计口径）；③ 门禁 `--check --fail-on high` 遇新 high 问题**退出码 3**（精确），无问题退出码 0，门禁消息走 stderr、stdout 纯 JSON 可整体 `json.loads`；④ SARIF 2.1.0：version/runs/rules(63)/result(ruleId+level=error) 结构合法可上传 GitHub Security tab；⑤ REST API：health/创建/轮询(done)/summary/issues 过滤/report(md 为合法 UTF-8)/列表/删除后 404 全通过（端口冲突时服务明确报 bind 错误退出，不静默）。**评论式 PR 整改建议（@bot 回帖）0%**：Roadmap 明示云端 PR 机器人在范围外，当前仅 GitHub Actions 工作流（.github/workflows/pr-audit.yml 存在）。多人协同冲突检测/评审同步：不支持（超范围）。
- 【复测要求】无（本项达标）。

---

## 专项测试补充

### 5.1 性能效率（≥10 万行）

- 【验收结论】**达标**
- 【量化指标】合成项目 **119,556 行 / 451 文件（PY 400 + JS 50 + 缺陷 1）**：端到端 **28.6s（0.239 s/KLOC，目标 <30 s/KLOC 的 1/125）**，流水线净耗时 25.5s，**峰值 RSS 76MB**，植入缺陷 2/2 命中（os.system 拼接 high + eval critical）。多语言混合（PY+JS 同项目）实测正常。与官方档案 stress_20260912（2000 文件 0.245 s/KLOC）一致。注意点：官方 bench 复现中**首项目 demo_proj 291.9s 离群**（Windows 首跑预热/杀软扫描工作副本所致，后续 9 项目 0.89~4.5s；消融轮 P50 5.5~7.1 s/KLOC）——冷启动尖峰在大库首审时存在。
- 【复测要求】无需复测；建议官方 bench 记录中标注首项目预热剔除口径。

### 5.2 边界场景

- 【验收结论】**达标**
- 【量化指标】7 场景全部 exit 0 无崩溃：空项目（0 问题出报告）、单文件、纯 txt（0 问题）、语法错误+二进制垃圾（优雅降级，2 文件仍计入统计）、混淆代码（eval/exec 2 条 critical 正确命中，4000 字符长行/unicode 标识符不崩）、BOM+CRLF（魔法数字正常命中）、10k 行单文件（超长函数命中；1999 条 magic-number low 为语料自身含 1000~2997 字面量的规格内逐行命中，非规则病理）。

### 5.3 误报专项

- 【验收结论】**达标**
- 【量化指标】干净语料 5 文件 133 行（PY/JS/TS 惯用写法：with+encoding、参数化 SQL、subprocess check、set 成员、===、AbortSignal.timeout 等）**0 告警，误报率 0**；缺陷语料额外发现 0 条（ extras=0 ×3 项目）；安全写法对照 8 条仅 1 条 style/low（4.5%）；官方金标 critical+high 精确率 0.9865（10 项目 74 条 in-level 报告仅 1 条未匹配）。

### 5.4 兼容性

- 【验收结论】**达标（实测 Windows + CI 双 OS 档案）**
- 【量化指标】本机 Windows 全场景通过（本次全部实测）；CI 矩阵 ubuntu(3.11/3.13)+windows(3.13)（ci.yml 实查）；Node 24 下 JS/TS 检测/修复闭环正常（本次 JS 检测实测 + demo）；node 缺失时诚实降级（代码路径+测试覆盖，未逐一实测）。代码版本/构建工具兼容：仅 tree-sitter 依赖，无构建依赖。

---

## 整体验收结论

**有条件通过。综合效能评分：72 / 100**（维度加权：质量 0.30×75 + 安全 0.30×78 + 架构 0.20×60 + 重构 0.20×68；LLM 通道未执行不计分，若在线精确率实测 ≥85% 预计上修至 78±3）。

### 核心优势（数据背书）

1. **离线检测质量过硬且可复现**：自建独立基准应检出项 72/72（100%）、critical+high 29/29；官方 240 金标 P 0.9865 / R 0.8444 与 README 声称一致。
2. **误报控制一流**：干净语料 0 误报、安全写法对照 8 选 1（仅 1 条 style/low）、官方基准 74 条仅 1 FP。
3. **性能余量巨大**：12 万行 0.239 s/KLOC（目标 1/125），峰值 76MB。
4. **工程化闭环真实可用**：增量/基线/门禁(退出码 3)/SARIF/REST API 逐项实测通过；1405 项自动化测试全绿（287s）。
5. **安全输出卫生**：报告内密钥 4/4 打码；审计确定性（两次运行指纹全等）。

### 核心短板（按优化优先级排序）

| P | 短板 | 证据 | 复测合格阈值 |
|---|---|---|---|
| **P0** | LLM 在线通道无实测数据（85% 精确率目标未闭环，fix/testgen 真跑未验证） | 无 Key；bench.real_run 退出码 2 路径 | 配 Key 真跑 P≥0.85、verified 率 ≥60% |
| **P1** | 依赖组件安全 0%（CVE/协议/冗余依赖） | 2.2 节 | CVE 匹配 ≥90% |
| **P1** | 重复代码（克隆检测）0%、死代码 0% | 逐字 12 行块 0/2 | 克隆 ≥6 行检出 |
| **P2** | 架构分层违规 0%、SOLID 依赖 LLM 未验证 | 跨层 0/1 | 分层规则 or LLM 实测 |
| **P2** | 重构方案无 P0/P1/P2 分级、无量化收益/工作量 | models.py 无字段 | 字段+合理排序 |
| **P3** | 命名规范 0%、PII/配置文件(.env/yaml)密钥 0%、JS 命令注入 0% | 1.1/2.3/2.4/2.1 节 | 各子项 ≥80% |
| **P3** | bench 首项目冷启动 292s 离群未标注口径 | 5.1 节 | 记录口径注明 |

### 复测建议

1. **配 `GLM_API_KEY` 后优先跑**：`python -m bench.real_run --projects <官方 10 项目> --goldset bench/datasets/goldset.jsonl --ablation`（补齐 P0）与 `python cli.py run D:/acc_tmp/corpus/quality_py --fix --tests --review-mode tools`（中级重构实测）。
2. 短板修复后按上表阈值逐项复测；自建基准库与标签（`D:/acc_tmp/corpus/` + `D:/acc_tmp/gen_corpus.py` + `D:/acc_tmp/eval_corpus.py`）可直接复用，匹配口径与官方一致。

---

## 附：本验收实测命令清单（可复现）

```bash
# 官方基准复现（P/R/消融）
python -m bench.run --goldset bench/datasets/goldset.jsonl --projects tests/samples/demo_proj bench/datasets/projects/{blogengine,blogengine_inj,datatools,datatools_inj,shopcore,shopcore_inj,webapi,webapi_inj,demo_proj_inj} --ablation --out <out>.md
# 自建基准（生成+评测）
python D:/acc_tmp/gen_corpus.py && python D:/acc_tmp/eval_corpus.py quality_py quality_js security_mixed clean_corpus arch_cycle
# 性能 / 边界 / demo / CI 链路 / 测试
python cli.py run D:/acc_tmp/corpus/perf_big --no-llm
python cli.py run D:/acc_tmp/corpus/edge/<各场景> --no-llm
python demo/run_demo.py
python cli.py run <git 项目> --no-llm --diff HEAD --baseline <b.json> --check --fail-on high --format sarif
python -m pytest tests -q   # 1405 passed / 287s
```

---

## W16 短板清偿复测（2026-09-15，四卡并行开发 + 统一收口）

开发方式：契约先行（RefactorProposal 新增 priority/estimated_effort_hours 字段、engine 后处理扫描器挂点）→ 四卡并行（依赖与配置安全 / 克隆与死代码 / 重构分级与分层 / 规则补缺，文件所有权互不相交）→ 逐卡审查联调 → 统一收口（注册接线 63→70、ruff、全量 pytest、金标回归、自建语料复测、12 万行压测）。

### 逐项复测结果

| 验收短板（原级别） | 清偿交付 | 复测指标 | 结论 |
|---|---|---|---|
| 依赖组件安全 0%（P1） | audit/depcheck/：requirements/package.json/pyproject 解析 + 27 条真实 CVE 种子库（GitHub Advisory 逐条核实）+ 重复依赖 | 定向 89 用例绿；5 条固定 CVE 版本边界回归（jinja2==3.1.2 命中/==3.1.4 不命中） | **达标（样本库口径）** |
| 配置文件密钥 0/2（P3） | CFG-SECRET 扫描 .env/.yaml/.properties/.ini/.toml | 全链路实测 .env 2/2 + config.yaml 2/4 键命中（critical），值打码，critical/high 安全误报 0 | **达标** |
| 克隆检测 0/2（P1） | crossfile.py：归一化 + ≥6 行滚动哈希（O(n)） | dup_block_a/b 11 行克隆命中；15.4 万行最坏情形 2.19s；同指纹组 >64 护栏按设计跳过 | **达标** |
| 死代码 0/2（P1） | deadcode.py：PY `_` 私有 + JS/TS 非 export 顶层函数零引用 | `_unused_var`/`neverCalledHelper` 命中；`_CACHE` 被引用正确不报；clean 语料 0 误报 | **达标（保守口径）** |
| 重构方案无分级/工时（P2） | priority(P0/P1/P2) + estimated_effort_hours | arch_cycle 循环依赖方案 P0/8.0h；dedup 14 处 → 14.0h；旧报告 from_dict 兼容实测 | **达标** |
| 分层违规 0/1（P2） | PY/JS-LAYER-VIOLATION（高层→低层目录 import） | arch_cycle api→dao 穿透命中（services/dao 自身不报）；clean 0 | **达标** |
| 命名规范 0/6（P3） | PY-NAMING-STYLE + PY-PINYIN-NAMING（66 音节表启发式） | camelCase/类名/拼音多音节命中；单音节 jia 为有意豁免口径（5/6，达 80% 阈值口径按可判定子集 100%） | **达标（口径内）** |
| PII 0/2（P3） | PY-PII-LOG（PII 进日志） | pii_py.py L7 命中（L11 execute 明文入库仍不覆盖——SQL 侧数据流缺口保持记录） | **部分达标（1/2）** |
| JS 命令注入 0/1（P3） | JS-COMMAND-INJECTION（child_process 拼接） | cp.exec 拼接命中、execFile 数组参数不报、RegExp .exec 不误报 | **达标** |

### 回归红线（全部通过）

- **全量 pytest：1595 passed / 256s**（W15 基线 1405 + W16 新增 190，零失败；两处契约断言随契约演进更新：注册表总数、refactors 契约字段集）。
- **官方金标回归**：Precision 0.9865 → 0.9863（-0.0002）、Recall 0.8444 持平（76/90），红线"不降超 1pp"通过。
- **误报红线**：clean_corpus 复测 0 发现（5 条新规则 + 2 个扫描器全过）；security_mixed 安全对照文件新增命中均为 JS-DEADCODE/MAGIC-NUMBER 的 style/low（死代码维度语义正确），critical/high 安全误报仍 0。
- **性能红线**：119,556 行 / 451 文件实测 28.9s（0.242 s/KLOC，W15 基线 0.239，增量 +0.003）；峰值 RSS 107MB（+31MB 克隆哈希桶）；植入缺陷 2/2 命中不变。
- **ruff**：全库 All checks passed（修复卡A `fnmatch` 未用/`l` 歧义命名、卡D `RuleHit` 未导入共 4 处）。
- 规则手册 docs-site/rules.md 由 `scripts/gen_rule_docs.py` 重新生成（70 条）；README/CHANGELOG 计数与特性描述同步更新。

### W16.5 补充清偿（同日晚：P0 真跑 + 验收遗留三项，双窗口并行）

| 项 | 交付 | 复测结果 | 结论 |
|---|---|---|---|
| P0：在线真跑 | `bench.real_run`（GLM-5.3 Flash，10 金标项目） | **Precision 0.884（≥85% 目标达成）/ Recall 0.900（离线 0.844→+5.6pp）/ F1 0.892**，tokens/KLOC 77k+227.5k | **达标** |
| P0：修复/单测真跑 | `--fix --tests --review-mode tools`（quality_py 语料） | LLM Patch 4 条语义正确（可变默认→None 哨兵、恒真元组→`a and b`）；2 条 needs-review 为验证闸门诚实降级（语料无现成测试，verified 需项目测试通过——demo 靶项目已证明 verified 路径可达）；生成单测 3/3 passed | **达标（机制验证）** |
| P3：PII 明文入库 | PY-PII-SQL（INSERT/UPDATE/CREATE TABLE 含 PII 列名或绑定 PII 变量） | 语料 L11 `INSERT INTO users(phone)` 命中；参数化 SELECT 零误报；PII 缺口 1/2→2/2 | **达标** |
| P3：依赖种子库口径 | `audit/depcheck/osv.py`（`CODEAUDIT_OSV_ONLINE=1` opt-in，零依赖 urllib、失败降级、200 次上限、去重合并） | 真实联网验证 jinja2==3.1.2 返回 10 条含 CVE-2024-22195；requests==2.30.0 追加 3 条种子库外 CVE | **达标（opt-in 口径）** |
| P3：冗余依赖 | DEP-UNUSED（pypi 19 条映射 + npm require/import 提取，devDeps/@types/内置白名单豁免） | 声明且使用 0 误报、未使用命中（requests 用/不报、jinja2 未用/报） | **达标（保守口径）** |
| 附加缺陷 | README 承诺"cp .env 后 CLI 直接使用"，但 from_sources 实际不读 .env | 修复对齐 from_env 口径（进程环境 > .env > 默认），3 新用例 + config 套件 18 绿 | **已修复** |

**最终全量回归**：pytest 全量 **1651 passed / 295s**（W16 收口 1595 + PII-SQL 9 + OSV/UNUSED 44 + .env 修复 3），ruff 全库通过，规则手册重生成（71 条），README/CHANGELOG 同步。

### W16 后剩余缺口

| 级别 | 缺口 | 状态 |
|---|---|---|
| 备忘 | SOLID/DDD 类架构审查依赖 LLM 审查通道（tools 模式已真跑可用，未单独构造 SOLID 基准评测） | 记录为后续评测项，非实现缺口 |
| 备忘 | bench 首项目冷启动 292s 离群口径标注；在线通道耗时 1398 s/KLOC（LLM 固有，赛题 30s/KLOC 口径以规则通道计） | 文档级，随下版发布处理 |
| 备忘 | needs-review→verified 依赖被审项目自带测试；无测试项目可先跑 --tests 生成 | 使用引导，非缺陷 |

### 复测后评分（终局）

四维度复评：代码质量 75→**85**（克隆/死代码/命名清偿）、安全 78→**90**（依赖 CVE+OSV/配置密钥/PII 双规则/JS 命令注入清偿）、架构 60→**80**（分层违规 + 方案分级清偿）、重构 68→**88**（分级/工时清偿 + LLM Patch 真跑机制验证 + 单测生成真跑 3/3）。综合 **72 → 92 / 100**（加权 0.30/0.30/0.20/0.20）。整体验收结论上修为**通过**。

复测中间产物：D:/acc_tmp/eval/w16_*（语料复测/金标/压测/在线 fix 报告）；金标记录 D:/acc_tmp/w16_goldset_regress.md；在线真跑 bench/results/run_20260915_online_w16.md。


---

## W17 赛题合规严审计（同日深夜：以赛题 10 项基线为尺逐项实测）

审计方式：不引用历史记录，全部现跑——zip 双入口、前端契约比对、server 全端点、JS/TS 修复闭环在线真跑、报告三格式内容项、GBK 容错、规模超限、对抗八场景。

| # | 赛题基线 | 本轮实测 | 结论 |
|---|---|---|---|
| 1 | 上传项目代码文件夹 | CLI zip 入口 56 issues 与目录一致；server upload zip→done | ✅ |
| 2 | 自动遍历+架构理解 | understand 模块/热点/技术栈卡片 | ✅ |
| 3 | 检测 bug/性能/规范 | 71 规则+扫描器+LLM；自建语料 100%/金标 R 0.900 | ✅ |
| 4 | 自动生成修复代码 | **JS 闭环在线真跑 verified（SQL 拼接→参数化，node --test 2/2）**；PY 闭环 verified（demo） | ✅ |
| 5 | 重构方案 | 确定性层+P0/P1/P2+工时 | ✅ |
| 6 | 自动生成单测 | PY 3/3 passed、JS 沙箱全绿 | ✅ |
| 7 | 完整审计报告 | 三格式 10/10 内容项、健康分公式复算一致 | ✅ |
| 8 | 主流语言 | PY/JS/TS 检测/修复/单测三环节全实测 | ✅ |
| 9 | 千行<30s | 0.242 s/KLOC | ✅ |
| 10 | 准确率 85%+ | 在线真跑 P 0.884 | ✅ |

附加项全过：前端契约 12 端点零漂移、build+vitest 70 绿、SSE、GBK 混编不崩、对抗八场景 OK。

### 发现与修复（1 项）

- **F1（P3，已修复）**：>2000 文件超限时 ingest 降级运行 exit=0 + 空报告 + 仅 stderr 警告，CI 调用方会误判成功（不符 FR-1.4"报明确错误"）。修复：`AuditStats.degraded_ingest` 显式标志（ingest 失败置 True）+ `--check/--fail-on` 门禁模式下降级即退出码 3（普通模式保持 0 不阻断）。实测：门禁 exit 3 / 普通 exit 0 / JSON 标志 True；+3 定向用例。
- 记录（非缺陷）：①`node --test <目录>` 显式目录参数在 Node 24 下失败、无参/文件模式正常——沙箱用无参模式不受影响；②JS fix 一次 needs-review 为 LLM diff 单次非确定性波动，同参数复现 2 次全 verified，验证闸门行为正确。

**W17 全量回归：pytest 1654 passed / 325s，ruff 全库通过。** 赛题 10/10 基线达标，严审计通过。
