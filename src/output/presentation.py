"""统一的表现层：集中所有容量/时长格式化调用和任务输出格式化"""

from collections.abc import Sequence
from datetime import timedelta
from typing import Protocol

import humanize

from src.output.logger import Logger


def fmt_size(size_bytes: float, binary: bool = True) -> str:
    return humanize.naturalsize(size_bytes, binary=binary)


def fmt_timespan(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0  # precisedelta 对负时长取绝对值，展示会失真；防御性钳制为 0
    return humanize.precisedelta(timedelta(seconds=seconds), minimum_unit="seconds")


class TaskStats(Protocol):
    """任务统计接口（结构化协议）。

    生产实现为 core.types.TranscodeStats；output 层不反向依赖 core，
    故以协议声明本模块消费的最小统计面。
    """

    @property
    def processed(self) -> int: ...

    @property
    def passthrough(self) -> int: ...

    @property
    def skipped(self) -> int: ...

    @property
    def failed(self) -> int: ...

    @property
    def expired(self) -> int: ...

    @property
    def total_bytes(self) -> int: ...


def print_task_header(
    *,
    logger: Logger,
    task_id: str | None,
    name: str | None,
    depends_on: Sequence[str] | None,
    mode: str,
    source: str,
    dest: str,
) -> None:
    """打印任务头部信息（参数由处理器从类型化配置提取）。"""
    if task_id:
        logger.info(f" - 标识: {task_id}")

    if name:
        logger.info(f" - 名称：{name}")

    if depends_on:
        logger.info(f" - 依赖: {'、'.join(depends_on)}")

    mode_labels = {
        "transcode": "转码",
    }
    mode_str = mode_labels.get(mode, mode)
    logger.info(f" - 任务模式: {mode_str}")
    logger.info(f" - 源目录: {source}")
    logger.info(f" - 目标目录: {dest}")


def print_task_summary(stats: TaskStats, duration: float, logger: Logger) -> None:
    """打印单轮任务的统计汇总。"""
    parts = [f"转码 {stats.processed} 项"]
    if stats.passthrough > 0:
        parts.append(f"直传 {stats.passthrough} 项")
    if stats.expired > 0:
        parts.append(f"过期清理 {stats.expired} 项")
    if stats.skipped > 0:
        parts.append(f"因多次失败跳过 {stats.skipped} 项")
    logger.info("，".join(parts) + "。")

    if stats.failed > 0:
        logger.info(f"失败 {stats.failed} 项。")

    if stats.total_bytes > 0:
        logger.info(
            f"共处理 {fmt_size(stats.total_bytes, binary=True)}，耗时 {fmt_timespan(duration)}。"
        )
