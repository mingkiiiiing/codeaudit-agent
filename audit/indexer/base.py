"""索引存储契约（契约文件，勿改）：T2 提供实现，T3/T4 消费。"""

from __future__ import annotations

from abc import ABC, abstractmethod

from audit.models import FunctionSlice, Reference, Symbol
from audit.workspace import WorkspaceContext


class IndexStore(ABC):
    """全库符号/依赖索引。实现必须支持增量（文件 sha256 未变则跳过重解析）。"""

    @abstractmethod
    def build(self) -> None:
        """扫描 workspace 并构建索引（幂等，可增量）。"""

    @abstractmethod
    def close(self) -> None:
        """释放资源（关闭 DB 连接等）。"""

    # -------- 查询

    @abstractmethod
    def symbols_for_file(self, rel_path: str) -> list[Symbol]:
        ...

    @abstractmethod
    def all_symbols(self) -> list[Symbol]:
        ...

    @abstractmethod
    def get_symbol(self, name: str, file_hint: str | None = None) -> Symbol | None:
        """按符号名查定义；file_hint 用于同名消歧。找不到返回 None。"""

    @abstractmethod
    def references(self, name: str, file_hint: str | None = None) -> list[Reference]:
        """符号的全部引用点（调用图优先，搜索兜底）。"""

    @abstractmethod
    def call_chain(self, symbol_name: str, direction: str = "callees", depth: int = 1) -> list[str]:
        """两跳内调用链，元素形如 'orders.py:88 create_order -> users.py:41 get_user'。"""

    @abstractmethod
    def dependencies(self, target: str, direction: str = "imports") -> list[str]:
        """target 为文件路径或模块名；direction ∈ {imports, imported_by}。"""

    # -------- 切片

    @abstractmethod
    def slices_for_file(self, rel_path: str) -> list[FunctionSlice]:
        """文件内全部函数级切片（每个函数一片）。"""

    @abstractmethod
    def grouped_slices(self, rel_path: str, max_lines: int = 400) -> list[FunctionSlice]:
        """把小切片合并成 ≤ max_lines 的组（供批量审查）；单函数超长则独立成片。"""

    # -------- 统计

    @abstractmethod
    def stats(self) -> dict:
        """files/symbols/call_edges/resolved_ratio 等汇总。"""
