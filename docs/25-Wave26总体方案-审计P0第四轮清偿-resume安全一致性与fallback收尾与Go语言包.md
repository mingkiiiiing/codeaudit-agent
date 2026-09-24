# Wave 26 总体方案：审计 P0 第四轮清偿（resume 安全一致性 / fallback 收尾 / Go 语言包）

> 依据：第六轮审计报告 `bench/results/audit_round6_20260919_复审.md`（72/100 B）P0 清单。
> 工作轮路由：第六轮审计（提示词一）完成 → 同对话工作轮（提示词二）。
> 执行方式：三卡并行后台代理 + 集成人统一联调（与 W23/W24/W25 同款流程）。

## 1. 范围裁决

| 审计 P0 项 | 本轮处置 | 理由 |
|---|---|---|
| P0-11 resume 安全一致性（F6-R1 + 限流定性 + 并发钉） | ✅ 卡 A | 半小时级、安全面、实证复现在案 |
| F5-R3 FakeLLM 离线警告观感 | ✅ 卡 A 顺带 | 同在 understand 层，最小侵入 |
| P0-7 收尾（fallback 事件接编排层 + from_sources env 层） | ✅ 卡 B | W25 已知边界清偿 |
| Go 语言包（P0-4 延伸，SUPPORTED_LANGUAGES 4→5） | ✅ 卡 C | Java 卡模式复制，语料双门验收 |
| P0-1 成本对数 | ⏸ 跳过 | 09-19 探针仍 HTTP 429（连续第四次实证），等配额窗口 |
| safe-rename / 结果版本化 | 顺延 | 下轮工作轮候选 |

## 2. 卡片交付（文件所有权互不相交）

- **卡 A = P0-11**：`server/app.py` resume 端点补 `_ensure_source_allowed(Path(config.source_path))`（**先校验后 mark_resuming，校验失败不改状态**；空 source_path 显式 400——`Path('')` resolve 恒等 cwd，交白名单会误导放行）；限流定性=**resume 已被全局 POST/DELETE 中间件覆盖（无排除清单），无需改代码，用例钉住**（429 实测断言）；`codeaudit` 双发 resume 语义钉住（恰一成功一拒绝，run_audit 恰一次）。F5-R3：architecture.py 对空文本响应降级 debug（首选 isinstance 方案被证伪——run_audit 会把 client 包进 _BudgetGateLLM，isinstance 恒 False；非空内容解析失败仍保留 warning 不回退 W15 可见性语义）。+5 server 用例、+2 understand 用例。
- **卡 B = P0-7 收尾**：`pipeline.py` 新增 `_emit_fallback_event`（run_audit finally 收口前，fallback_used>0 → warning 事件含次数与 fallback_model 结构化字段；unwrap 后 getattr 判定，_BudgetGateLLM 零改动，无属性客户端自然零事件）；`config.py` from_sources env 层接入 GLM_MODEL_FALLBACK（三层合成完整）。+3 pipeline 用例、+4 config 用例。
- **卡 C = Go 语言包**：`audit/indexer/go_extract.py`（package/const/func 含 receiver `Type.Method`/`(*Type).Method`、import 单行+圆括号块、调用点；已知边界 docstring 明示：func_literal 体内、泛型实例化、内建函数不记）；parsers.py SUPPORTED_LANGUAGES 4→5；engine `_AST_LANGUAGES` 扩 go；`audit/detect/rules/go.py` 三规则（GO-SQL-INJECTION / GO-HARDCODED-SECRET / GO-LONG-FUNCTION），**规则库 86→89 条**；`tests/corpus/go/` 13 文件（4 正例 + 9 干净反例，internal 分层布局）；+22 indexer 用例、+27 规则用例。**超卡面最小必要接线（集成人已核可，均非契约文件）**：utils.py `_EXTENSION_LANGUAGE` 补 .go、indexer/store.py 三处语言分派（不补则 resolved_ratio=0）。

## 3. 集成人联调记录

- 改动面核对：三卡文件与申报逐项吻合；卡 C 两处超清单接线均为达成验收的必要项且非契约文件，接受并报备。
- **联调发现并修复 1 处集成冲突**：`tests/unit/utils/test_utils.py` 既有参数化断言 `.go → None`（未登记扩展名时代）——卡 C 接入 .go 后过时；卡 C 定向子集（indexer/detect）未覆盖 utils 目录故自验未拦。修复=断言更新为 `.go → "go"`（集成人）。全量首跑 1 failed / 2238 passed → 修复后重跑。
- 集成人收口三件：①`scripts/gen_rule_docs.py` 再生 `docs-site/rules.md` **89 条**；②router.py docstring 过时表述同步（事件标注已接编排层，W26 卡 B）；③卡 B 报备裁决：GLM_MODEL_FALLBACK 暂不扩入 `.env` 白名单 `_DOTENV_GLM_KEYS`（会同时改变 from_env 行为，涉及 W15 键值白名单安全语义，留待显式决策）。
- 统一联调终验（最终树）：
  - 全量 pytest **2239 passed, 2 skipped**（305s 量级）＝基线 2176 + 净新增 63（卡A 7 / 卡B 7 / 卡C 49），联调冲突修复后全绿；
  - ruff 全域绿（audit cli.py server bench）；demo 闭环 **verified 8.8s**；
  - 金标一条命令复跑 **P=1.000 / R=0.844 / F1=0.916**（240 条，`w26_goldset_20260919.md`）——Go 三条新规则零金标污染，存量零回退门连续四轮成立；
  - Go 语料终树 E2E 复核：**6 命中逐位一致**（SQL3/密钥2/长函数1）+ 9 反例零误报 + AST 13/13 + resolved_ratio 0.4（新增门达标）；
  - soak 300s 压测判定见 `bench/results/soak_w10.md`。
- P0-1：09-19 探针仍 429（连续第四次），w22_focus_compare 对数继续阻塞，如实记档。

## 4. 已知边界（如实申报）

1. doctor 不探测 go 语言包（cli.py `_DOCTOR_LANGS` 硬编码清单，本轮 cli.py 归属其他卡禁改；后续波次追加 `("go", False)` 即可）。
2. Go 跨文件调用解析为"尽力解析"：接收者变量调用（`u.Save()`）不做类型推断，保留未解析边（有测试固化）。
3. GLM_MODEL_FALLBACK 不读 .env（白名单未扩，见 §3 裁决③）；仅真实进程环境变量与配置文件（.codeaudit.toml fallback_model 键本就放行）两路可用。
4. F5-R3 采用空文本降级方案：非空内容的解析失败/网络异常仍打 warning（W15 可见性语义保留）。

## 5. 下一轮入口

- **下轮路由=待用户新开 GLM-5.3 对话贴提示词一（第七轮审计）**，复审重点：①三卡新增面（resume 白名单复验的顺序与空路径防御、fallback 事件挂点 finally 的语义、go_extract 口径与 store 分派）；②P0-1 对数状态（第五次探针）；③soak 36/36 收口结论。
- 工作轮候选：①P0-1（等窗口）；②safe-rename 确定性原语 MVP；③结果版本化；④doctor 补 go；⑤W14~W26 全量提交 + push + 打版 0.7.0（用户侧，积压 26 改动 + 10 新文件）。
