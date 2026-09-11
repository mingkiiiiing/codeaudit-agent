# 03 Agent 工具集与 Prompt 设计

版本：v1.0 ｜ 日期：2026-09-11
本文档是项目"灵魂"：工具定义 + System Prompt 全文可直接进入实现，也是面试深挖时的核心弹药。

---

## 1. 工具集总览（10 个）

| # | 工具 | 职责 | 使用角色 |
|---|---|---|---|
| 1 | `list_files` | 列目录树/按模式过滤 | Review / Verify |
| 2 | `read_file` | 读文件（行号标注，支持行区间） | 全部 |
| 3 | `search_code` | 正则/文本全库搜索（ripgrep 语义） | 全部 |
| 4 | `get_symbol` | 按符号名取定义（源码+签名+位置） | Verify / Fix / TestGen |
| 5 | `find_references` | 查符号全部引用点（基于调用图+搜索兜底） | Verify |
| 6 | `get_call_chain` | 两跳内调用链查询 | Verify |
| 7 | `get_dependencies` | 模块/文件依赖查询 | Review |
| 8 | `record_issues` | 结构化提交本轮发现（唯一写入口） | Review / Verify |
| 9 | `submit_patch` | 提交 unified diff | Fix |
| 10 | `run_tests` | 白名单命令跑测试（沙箱） | Fix / TestGen |

设计原则：
- **只读工具占 7/10**——审计 Agent 的主业是"看"，写能力严格收敛到三个出口（问题记录、补丁、跑测试），杜绝"顺手改代码"。
- 每个工具都有**明确的失败语义**（返回 `error` 字符串而非抛异常），Agent 能读到失败原因并自纠。
- 返回内容全部**带行号、带截断**（单次返回 > 200 行自动截断并提示续读区间），控上下文长度。

---

## 2. 工具 JSON Schema 完整定义

> 格式即 GLM/OpenAI Function Calling 的 `tools` 参数，可直接使用。

### 2.1 list_files
```json
{
  "type": "function",
  "function": {
    "name": "list_files",
    "description": "列出项目文件。可按目录前缀或 glob 模式过滤。返回相对路径列表。",
    "parameters": {
      "type": "object",
      "properties": {
        "prefix": {"type": "string", "description": "目录前缀，如 'app/services'"},
        "pattern": {"type": "string", "description": "glob 模式，如 '*.py'"}
      },
      "required": []
    }
  }
}
```

### 2.2 read_file
```json
{
  "type": "function",
  "function": {
    "name": "read_file",
    "description": "读取文件内容，每行前带行号（'12: code'）。大文件自动截断到 200 行，返回值会提示剩余区间。",
    "parameters": {
      "type": "object",
      "properties": {
        "path": {"type": "string", "description": "相对项目根的路径"},
        "start_line": {"type": "integer", "description": "起始行（1-based，含）"},
        "end_line": {"type": "integer", "description": "结束行（含），单次最大 200 行"}
      },
      "required": ["path"]
    }
  }
}
```

### 2.3 search_code
```json
{
  "type": "function",
  "function": {
    "name": "search_code",
    "description": "全库正则/文本搜索，返回 'file:line: 匹配行' 列表（最多 50 条）。用于确认某模式的所有出现位置。",
    "parameters": {
      "type": "object",
      "properties": {
        "query": {"type": "string", "description": "正则表达式或普通文本"},
        "file_glob": {"type": "string", "description": "可选，限定文件范围如 'app/**/*.py'"},
        "is_regex": {"type": "boolean", "default": true}
      },
      "required": ["query"]
    }
  }
}
```

### 2.4 get_symbol
```json
{
  "type": "function",
  "function": {
    "name": "get_symbol",
    "description": "按名称取代码符号（函数/类/方法）的定义：源码、签名、所在文件与行号。找不到时返回近似候选名。",
    "parameters": {
      "type": "object",
      "properties": {
        "name": {"type": "string", "description": "符号名，如 'get_user' 或 'UserService.create'"},
        "file_hint": {"type": "string", "description": "可选，限定定义所在文件"}
      },
      "required": ["name"]
    }
  }
}
```

### 2.5 find_references
```json
{
  "type": "function",
  "function": {
    "name": "find_references",
    "description": "查找符号在项目中的全部引用位置（调用点），基于预建调用图，未解析引用以搜索兜底。用于评估影响面与取证。",
    "parameters": {
      "type": "object",
      "properties": {
        "name": {"type": "string"},
        "file_hint": {"type": "string", "description": "定义所在文件，消歧同名符号"}
      },
      "required": ["name"]
    }
  }
}
```

### 2.6 get_call_chain
```json
{
  "type": "function",
  "function": {
    "name": "get_call_chain",
    "description": "查询某函数向上（谁调它）或向下（它调谁）两跳的调用链，输出 'A.f → B.g' 路径列表。",
    "parameters": {
      "type": "object",
      "properties": {
        "symbol": {"type": "string"},
        "direction": {"type": "string", "enum": ["callers", "callees"], "default": "callees"},
        "depth": {"type": "integer", "minimum": 1, "maximum": 2, "default": 1}
      },
      "required": ["symbol"]
    }
  }
}
```

### 2.7 get_dependencies
```json
{
  "type": "function",
  "function": {
    "name": "get_dependencies",
    "description": "查询文件或模块的导入依赖与被依赖关系。",
    "parameters": {
      "type": "object",
      "properties": {
        "target": {"type": "string", "description": "文件路径或模块名"},
        "direction": {"type": "string", "enum": ["imports", "imported_by"], "default": "imports"}
      },
      "required": ["target"]
    }
  }
}
```

### 2.8 record_issues（写入口）
```json
{
  "type": "function",
  "function": {
    "name": "record_issues",
    "description": "提交本文件审查结论。必须恰好调用一次：有问题提交问题数组，无问题提交空数组。行号必须来自你读到的文件内容，禁止猜测。",
    "parameters": {
      "type": "object",
      "properties": {
        "issues": {
          "type": "array",
          "items": {
            "type": "object",
            "properties": {
              "category": {"type": "string", "enum": ["bug", "performance", "style", "security"]},
              "severity": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
              "title": {"type": "string", "description": "一句话问题标题（中文）"},
              "file": {"type": "string"},
              "line_start": {"type": "integer"},
              "line_end": {"type": "integer"},
              "description": {"type": "string", "description": "为什么是问题、触发条件、后果（中文）"},
              "evidence": {"type": "array", "items": {"type": "string"},
                           "description": "证据，如 'orders.py:88 调用 get_user；users.py:41 return None'"},
              "suggestion": {"type": "string", "description": "修复建议（中文）"},
              "confidence": {"type": "number", "minimum": 0, "maximum": 1}
            },
            "required": ["category", "severity", "title", "file", "line_start", "line_end",
                         "description", "suggestion", "confidence"]
          }
        }
      },
      "required": ["issues"]
    }
  }
}
```

### 2.9 submit_patch
```json
{
  "type": "function",
  "function": {
    "name": "submit_patch",
    "description": "提交针对单个 Issue 的修复，unified diff 格式（git apply 可解析）。只允许改动与该 Issue 直接相关的行。",
    "parameters": {
      "type": "object",
      "properties": {
        "issue_id": {"type": "string"},
        "diff": {"type": "string", "description": "unified diff，含 ---/+++ 头与 @@ hunk"},
        "rationale": {"type": "string", "description": "修复思路说明（中文，将写入报告）"}
      },
      "required": ["issue_id", "diff", "rationale"]
    }
  }
}
```

### 2.10 run_tests
```json
{
  "type": "function",
  "function": {
    "name": "run_tests",
    "description": "在沙箱中运行测试（白名单：pytest / jest）。返回退出码与末尾 80 行输出。60 秒超时。",
    "parameters": {
      "type": "object",
      "properties": {
        "target": {"type": "string", "description": "测试文件/用例路径，空为全部"},
        "framework": {"type": "string", "enum": ["pytest", "jest"]}
      },
      "required": ["framework"]
    }
  }
}
```

---

## 3. System Prompt 全文

### 3.1 Review Agent（文件审查）

```text
你是资深代码评审员，正在审计一个真实项目。项目背景：
<architecture_card>
{架构卡片摘要}
</architecture_card>

当前任务：审查文件 {file_path}（{loc} 行）。下面附上该文件的符号表与源码，
以及静态规则扫描的初步命中（hint，可能含误报，需你独立判断）：

[file_symbols]
{本文件符号表}

[file_source]
{源码（带行号）}

[rule_hints]
{规则命中列表，无则写"无"}

可用的工具：read_file / search_code / get_symbol / find_references /
get_call_chain / get_dependencies / record_issues。

审查要求：
1. 只报告你能在代码中指认的真实问题，严禁臆测不存在的代码。每条问题的
   行号必须与上面读到的内容一致。
2. 对每个疑似问题，若涉及其他文件的函数行为（如"可能返回 None"），必须
   用 get_symbol 或 read_file 查看对方源码核实后再定性，并在 evidence 中
   写明证据位置；查证不到就在 confidence 中如实降低。
3. severity 判级：critical=必然错误或安全漏洞；high=特定条件下出错的缺陷
   或显著性能问题；medium=可维护性；low=风格。
4. 不要报告"建议加注释/加文档"这类无行为影响的问题（除非安全/密钥相关）。
5. 审查完成后，恰好调用一次 record_issues：无问题就提交空数组。
   不要用文字总结代替工具调用。
```

### 3.2 Verify Agent（证据核验）

```text
你是审计质检员。下面是一条待确认的问题报告，你的职责是独立复核它，
宁可驳回也不放过似是而非的结论。

[issue] {issue JSON}

复核清单：
1. 行号与代码事实核对：read_file 查看报告行，确认代码与描述一致。
2. 触发条件核实：问题描述的输入/状态是否真实可达？用 search_code /
   find_references 检查调用方是否已经做了防护（判空/try/校验）——若所有
   调用方都防护了，判 false_positive。
3. 跨文件行为核实：若结论依赖其他文件函数的返回行为，必须读对方源码。
4. 给出裁定：confirmed（附证据链）/ false_positive（附驳回理由）/
   uncertain（附缺失的信息），并给出最终 severity 与 confidence。

裁定通过调用 record_issues 提交（驳回的问题也提交，severity 保持原值，
confidence 置 0 并在 description 注明驳回原因，供报告统计误报率）。
```

### 3.3 Fix Agent（修复生成）

```text
你是修复工程师。针对以下已确认问题生成最小修复：
[issue] {issue JSON，含证据链}
[context] {相关文件切片与已读到的关联符号}

要求：
1. 最小改动：只改与该问题直接相关的行，不顺手重构、不改格式。
2. 保持项目现有风格（命名/异常处理/日志用法向周边代码看齐）。
3. 输出 unified diff（git apply 可解析），随后调用 submit_patch 提交。
4. rationale 用中文说明修复思路与为何不影响其他调用方（用证据说话）。
```

### 3.4 TestGen Agent（单测生成）

```text
你是测试工程师，为以下函数生成 pytest 回归测试：
[target] {函数源码与签名}
[dependencies] {依赖符号源码}
[fixed_version] 若该函数刚被修复，附修复 diff，重点覆盖修复点回归
[style_example] {项目现有测试示例，无则省略}

要求：
1. ≥ 5 条用例：正常路径 ≥ 2、边界 ≥ 2（空输入/极值/空集合）、异常 ≥ 1。
2. 每条用例必须有显式 assert，断言值需你根据函数语义推演，禁止 assert True。
3. 不 mock 被测函数自身；仅对 IO/网络/时间等外部依赖使用 monkeypatch。
4. 直接输出完整测试函数代码（import 自包含），不要解释文字。
5. 若代码不可单测（强耦合全局状态），输出 "# UNTESTABLE: 原因" 并说明。
```

---

## 4. Prompt 工程要点（实现与调优备忘）

| 问题 | 对策 |
|---|---|
| 幻觉行号 / 编造代码 | prompt 硬约束"行号必须来自读到的内容" + 代码层行号校验器（行存在 + 标识符交集），不过即丢 |
| 硬凑问题（无病呻吟） | "无问题提交空数组"显式许可 + 置信度 <0.6 且无规则佐证即丢弃 + 报告端统计每文件 issue 密度异常检测 |
| JSON 解析失败 | GLM JSON Mode；解析失败自动将报错回喂重试 1 次；仍失败降级正则抽取 |
| 同一问题重复报告 | 合并键 `(file, category, line±5)`；Verify 阶段二次去重 |
| 严重度虚高 | prompt 给出判级定义 + 校准集回归：每次 prompt 改动跑 mini-bench（20 个已标注样本）防退化 |
| 遗忘输出格式 | 关键指令放在 prompt 首尾双写；工具描述中重复强制"恰好调用一次 record_issues" |

## 5. 与既有框架的适配清单（工程排期参照）

1. `LLMClient`：GLM OpenAI 兼容端点接入、`tools` 参数透传、流式可选、token 用量回读。
2. `AgentRuntime` 增强项：`max_iterations` / `token_budget` 熔断、工具调用超时、`on_tool_call` 事件回调（供 SSE 进度）。
3. 工具 Handler 实现：全部落在 `audit/agent/tools/`，统一签名 `async def handler(**kwargs) -> str`，内部访问 `WorkspaceContext`（工作副本根路径、索引连接、沙箱句柄）。
4. 失败语义统一：handler 永不抛异常，返回 `{"error": "..."}`；框架把 tool 结果以 `role=tool` 消息回填。
