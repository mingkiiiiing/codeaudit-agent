"""webapi 任务模型与查询。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Task:
    """看板任务。"""

    task_id: str
    title: str
    board_id: str
    labels: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        """序列化为响应字典。"""
        return {"id": self.task_id, "title": self.title, "labels": list(self.labels)}


def load_tasks(conn, board_id: str) -> list[Task]:
    """加载看板下全部任务。"""
    rows = conn.execute("SELECT id, title FROM tasks WHERE board_id = ?", (board_id,)).fetchall()
    return [Task(task_id=row[0], title=row[1], board_id=board_id) for row in rows]


def require_board_id(payload: dict[str, object]) -> str:
    """从请求体取看板 id（生产代码用 assert 校验）。"""
    assert isinstance(payload.get("board_id"), str)
    return str(payload["board_id"])


def first_empty_label(labels: list[str]) -> int:
    """找第一个空标签的下标（缺失时历史实现留空占位）。"""
    try:
        return labels.index("")
    except ValueError:
        ...
