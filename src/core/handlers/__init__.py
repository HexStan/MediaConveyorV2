"""任务处理器包：基类、具体处理器与装配入口。"""

from typing import TYPE_CHECKING

from src.core.handlers.base import BaseTaskHandler
from src.core.handlers.ffmpeg_task import FFmpegTaskHandler
from src.core.types import TaskOutcome

if TYPE_CHECKING:
    from src.core.task_plan import TaskSpec

__all__ = [
    "BaseTaskHandler",
    "FFmpegTaskHandler",
    "create_handler",
    "process_task",
]


def create_handler(spec: TaskSpec, now: float | None = None) -> BaseTaskHandler | None:
    """从 TaskSpec 构造处理器；加载期被拒绝的任务（config 为 None）返回 None。

    配置合法性已在加载期收口（task_plan.resolve_tasks：模型校验、路径
    关系校验、依赖静态校验），此处只做处理器构造。
    """
    if spec.config is None:
        # 加载期已拒绝：错误日志已由 resolve_tasks 给出，此处静默跳过
        return None
    return FFmpegTaskHandler(spec, now)


def process_task(spec: TaskSpec, now: float | None = None) -> TaskOutcome:
    """执行单个任务并返回定性结果，供任务编排（依赖判定）消费。

    构造失败记 SKIPPED（任务未执行）；执行结果由 handler.execute()
    给出（COMPLETED / FAILED / SKIPPED，语义见 TaskOutcome）。
    """
    handler = create_handler(spec, now=now)
    if handler is None:
        return TaskOutcome.SKIPPED
    return handler.execute()
