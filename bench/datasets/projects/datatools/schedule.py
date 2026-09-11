"""datatools 批次调度：重试与告警。"""

from __future__ import annotations

import logging

LOGGER = logging.getLogger(__name__)


def run_job(job: str) -> bool:
    """执行单个批次任务（演示桩：失败由调用方重试）。"""
    return len(job) > 0


def retry_batch(job: str, attempts: int = 3) -> bool:
    """带重试地执行批次任务。"""
    for _ in range(attempts):
        if run_job(job):
            return True
    LOGGER.warning("batch %s failed after retries", job)
    LOGGER.error("batch %s failed after %d attempts", job, attempts)
    return False
