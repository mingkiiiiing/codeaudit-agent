"""核心数据模型：全部阶段共享的数据结构（契约文件，勿改，变更走集成人）。

所有数据类均带 to_dict()/from_dict()，枚举字段自动与字符串互转。
"""

from __future__ import annotations

import dataclasses
import enum
from dataclasses import dataclass, field
from typing import Any, get_args, get_origin, get_type_hints

# ---------------------------------------------------------------- 枚举


class Category(str, enum.Enum):
    BUG = "bug"
    PERFORMANCE = "performance"
    STYLE = "style"
    SECURITY = "security"


class Severity(str, enum.Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class IssueSource(str, enum.Enum):
    RULE = "rule"
    LLM = "llm"
    RULE_LLM = "rule+llm"


class FixStatus(str, enum.Enum):
    """Issue 的修复状态（R1-21：删除从未被赋值的死枚举 PATCH_GENERATED，
    补丁生成计数由 ctx.extra["fix_stats"]["patch_generated"] 承载）。"""

    NONE = "none"
    VERIFIED = "verified"
    NEEDS_REVIEW = "needs-review"
    SYNTAX_OK = "syntax-ok"


SEVERITY_WEIGHT: dict[str, float] = {
    "critical": 10.0,
    "high": 5.0,
    "medium": 2.0,
    "low": 0.5,
}

# ---------------------------------------------------------------- 序列化基类


def _sanitize(obj: Any) -> Any:
    """递归把 Enum 转为 value，保证可 JSON 序列化。"""
    if isinstance(obj, enum.Enum):
        return obj.value
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    return obj


def _coerce(value: Any, tp: Any) -> Any:
    """from_dict 时按字段类型递归 coercing：枚举字符串、嵌套模型、容器元素。"""
    if value is None:
        return None
    origin = get_origin(tp)
    args = get_args(tp)
    non_none = [a for a in args if a is not type(None)]
    if origin is None:
        if isinstance(tp, type) and issubclass(tp, enum.Enum):
            try:
                return tp(value)
            except ValueError:
                return value
        if isinstance(tp, type) and issubclass(tp, _Model) and isinstance(value, dict):
            return tp.from_dict(value)
        return value
    if args and type(None) in args and len(non_none) == 1:  # Optional[X]
        return _coerce(value, non_none[0])
    if origin in (list, tuple) and non_none:
        return [_coerce(v, non_none[0]) for v in value]
    if origin is dict and len(non_none) == 2:
        return {k: _coerce(v, non_none[1]) for k, v in value.items()}
    return value


@dataclass
class _Model:
    def to_dict(self) -> dict[str, Any]:
        return _sanitize(dataclasses.asdict(self))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "_Model":
        hints = get_type_hints(cls)
        kwargs: dict[str, Any] = {}
        for f in dataclasses.fields(cls):
            if f.name not in data:
                continue
            kwargs[f.name] = _coerce(data[f.name], hints.get(f.name, Any))
        return cls(**kwargs)


# ---------------------------------------------------------------- Stage1/2 产物


@dataclass
class FileManifest(_Model):
    path: str = ""  # 相对 src_root 的 posix 路径
    language: str = ""
    loc: int = 0
    sha256: str = ""
    parsed_ok: bool = True
    skipped_reason: str = ""


@dataclass
class Symbol(_Model):
    id: str = ""
    file: str = ""
    kind: str = ""  # function | method | class | constant
    name: str = ""  # 简名，如 get_user；类方法用 Class.method
    line_start: int = 0
    line_end: int = 0
    signature: str = ""


@dataclass
class Reference(_Model):
    file: str = ""
    line: int = 0
    snippet: str = ""


@dataclass
class ImportRecord(_Model):
    file: str = ""
    module: str = ""
    name: str = ""  # 导入的符号名；import a.b 时为空
    alias: str = ""


@dataclass
class CallEdge(_Model):
    caller_file: str = ""
    caller_symbol: str = ""
    callee_name: str = ""
    callee_file: str = ""  # 解析成功时为目标文件
    callee_symbol: str = ""
    resolved: bool = False


@dataclass
class FunctionSlice(_Model):
    """函数级切片：LLM 审查与上下文注入的基本单位。"""

    file: str = ""
    symbol: str = ""  # 限定名，如 OrdersService.create
    line_start: int = 0
    line_end: int = 0
    code: str = ""
    signature: str = ""
    kind: str = "function"


# ---------------------------------------------------------------- Stage4 检测


@dataclass
class RuleHit(_Model):
    rule_id: str = ""
    category: Category = Category.BUG
    severity: Severity = Severity.LOW
    file: str = ""
    line_start: int = 0
    line_end: int = 0
    message: str = ""
    snippet: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Issue(_Model):
    id: str = ""
    category: Category = Category.BUG
    severity: Severity = Severity.LOW
    title: str = ""
    file: str = ""
    line_start: int = 0
    line_end: int = 0
    code_snippet: str = ""
    description: str = ""
    evidence: list[str] = field(default_factory=list)
    suggestion: str = ""
    confidence: float = 0.0
    source: IssueSource = IssueSource.RULE
    fix_status: FixStatus = FixStatus.NONE
    patch_id: str = ""


# ---------------------------------------------------------------- Stage5/6 产物


@dataclass
class Patch(_Model):
    id: str = ""
    issue_id: str = ""
    diff: str = ""
    rationale: str = ""
    apply_status: str = "pending"  # pending | verified | needs-review | syntax-ok | failed
    tests_run: int = 0
    tests_passed: int = 0
    # W19（审计 P1 清偿）：接口兼容性比对结果——apply 前后公开函数签名 diff 的
    # 破坏性变更清单（如"移除函数 foo""变更签名 bar(a,b) -> bar(a,b,c)"）。
    # 空列表 = 未检测到破坏性变更（向后兼容，旧报告 from_dict 容忍缺字段）。
    compat_notes: list[str] = field(default_factory=list)
    # P0-2（apply-to-source 预览确认回路）：补丁目标文件的原文指纹——键为相对项目根
    # 的 posix 路径，值为该文件在补丁生成时（apply 前）内容的 sha256（hex）。
    # 用途：apply 回用户源码前核对目标文件未被后续改动（防覆盖）。新增文件记 ""
    # （目标须不存在）。空 dict = 旧版产物无指纹（dry-run 可预览，落盘被拒绝并
    # 提示重新审计）。带默认值：from_dict 对旧报告缺字段兼容，契约零破坏。
    target_sha256: dict[str, str] = field(default_factory=dict)


@dataclass
class TestCase(_Model):
    id: str = ""
    target: str = ""  # 被测符号限定名
    file: str = ""  # 生成文件相对路径
    status: str = "pending"  # pending | passed | dropped
    kind: str = "normal"  # normal | boundary | error
    assert_count: int = 0


# ---------------------------------------------------------------- 契约 v1.7：重构方案（W7-A2）


@dataclass
class RefactorProposal(_Model):
    """重构方案（契约 v1.7，赛题要求 5「自动生成重构方案」的结构化产物）。

    由 audit.refactor 阶段（detect 之后、fix 之前）产出：
    - 确定性启发式层从既有索引与规则命中聚合（source="heuristic"）；
    - LLM 增强层对 top N 方案深化 rationale/steps（source 升级 "heuristic+llm"）。
    """

    id: str = ""
    title: str = ""
    target: str = ""  # 重构目标（文件/符号/模块），如 "app/services/orders.py::create_order"
    kind: str = "other"  # dedup | decompose | split-module | simplify | other
    rationale: str = ""  # 为什么重构
    steps: list[str] = field(default_factory=list)  # 操作步骤
    benefits: str = ""  # 收益一句话
    related_issues: list[str] = field(default_factory=list)  # 关联 Issue id
    source: str = "heuristic"  # heuristic | llm | heuristic+llm
    confidence: float = 0.0
    # W16（验收短板清偿 P2）：优先级与工作量估算。priority ∈ {"P0","P1","P2"}，
    # 空 = 不分级（保持旧行为）；estimated_effort_hours 为启发式估算工时（0 = 未估算）。
    # 两字段均带默认值：from_dict 对旧报告缺字段兼容，报告 schema 不破坏。
    priority: str = ""
    estimated_effort_hours: float = 0.0


# ---------------------------------------------------------------- Stage3/7 产物


@dataclass
class ArchitectureCard(_Model):
    text: str = ""  # 人读摘要
    tech_stack: list[str] = field(default_factory=list)
    modules: dict[str, str] = field(default_factory=dict)  # 目录 -> 职责摘要
    hotspots: list[str] = field(default_factory=list)  # 风险热点文件/符号


@dataclass
class AuditStats(_Model):
    duration_sec: float = 0.0
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    files_total: int = 0
    loc_total: int = 0
    suppressed: int = 0  # 被基线抑制的问题数（契约 v1.4）
    # W17（赛题合规严审计 F1）：ingest 未成功（如超 FR-1.4 规模上限）时的降级运行标志。
    # 显式机器可读——此前只有 stderr 警告与 files_total=0 的间接信号，CI 调用方
    # 解析 stdout JSON 时无法区分"空项目"与"审计未实际执行"。默认 False 向后兼容。
    degraded_ingest: bool = False


@dataclass
class AuditReport(_Model):
    audit_id: str = ""
    project_name: str = ""
    languages: dict[str, float] = field(default_factory=dict)  # 语言 -> 占比
    loc: int = 0
    health_score: float = 0.0
    summary: dict[str, int] = field(default_factory=dict)  # severity -> count
    issues: list[Issue] = field(default_factory=list)
    patches: list[Patch] = field(default_factory=list)
    test_cases: list[TestCase] = field(default_factory=list)
    refactor_proposals: list[RefactorProposal] = field(default_factory=list)  # 契约 v1.7
    architecture: ArchitectureCard | None = None
    stats: AuditStats = field(default_factory=AuditStats)
    created_at: str = ""
    schema_version: str = "1.0"  # 报告格式版本（契约 v1.4）
    # W-P5（审计 P0-5 清偿）：测试覆盖盲区（untested_hotspots）计算结果，
    # 结构见 audit/report/testcoverage.py（available/eligible_files/untested_files/
    # ratio/items/methodology 等）。None = 未计算（索引不可用或旧报告）。
    # 带默认值的可选字段：from_dict 对缺字段的老报告兼容（默认值零变化，schema 不破）。
    untested_hotspots: dict[str, Any] | None = None


def count_by_severity(issues: list[Issue]) -> dict[str, int]:
    counts = {s.value: 0 for s in Severity}
    for issue in issues:
        counts[issue.severity.value if isinstance(issue.severity, Severity) else str(issue.severity)] += 1
    return counts


def health_score(issues: list[Issue], loc: int, kloc_scale: float = 50.0) -> float:
    """健康分：score = max(0, 100 - 加权问题密度 * kloc_scale)。系数标定见 docs/04。"""
    if loc <= 0:
        loc = 1
    weighted = sum(
        SEVERITY_WEIGHT.get(i.severity.value if isinstance(i.severity, Severity) else str(i.severity), 0.0)
        for i in issues
    )
    return round(max(0.0, 100.0 - weighted / (loc / 1000.0) * kloc_scale), 1)
