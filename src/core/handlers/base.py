"""任务处理器基类：模板方法执行骨架。

子类只实现 ``_execute()``：任务头展示等共有前导步骤收敛于此，
新增模式自动获得同一入口行为。外部条件校验（目录存在等运行期才可
判定的条件）由各处理器在 ``_execute()`` 内自行检查；配置可判定的
校验已在加载期收口（task_config / task_plan）。
"""

import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from src.app.context import AppContext
from src.core.types import TaskOutcome
from src.output.presentation import print_task_header

if TYPE_CHECKING:
    from src.core.task_config import TaskConfig
    from src.core.task_plan import TaskSpec


class BaseTaskHandler(ABC):
    def __init__(
        self,
        spec: TaskSpec,
        now: float | None = None,
    ) -> None:
        config = spec.config
        assert config is not None, "config 为 None 的 TaskSpec 不应进入处理器构造"
        self.task: TaskConfig = config
        self.depends_on = spec.depends_on
        self.now = now or time.time()
        self.source_root: str = config.source_dir
        self.dest_root: str = config.dest_dir
        self.task_mode: str = config.mode

    def execute(self) -> TaskOutcome:
        """模板方法：任务头展示 → 子类执行。

        返回本轮执行的定性结果（TaskOutcome）：配置校验失败的任务在
        加载期已被拒绝（不会构造处理器）；子类执行是否完整跑完由
        _execute() 的布尔结果定性。
        """
        ctx = AppContext.get()

        print_task_header(
            logger=ctx.logger,
            task_id=self.task.id,
            name=self.task.name,
            depends_on=self.depends_on,
            mode=self.task.mode,
            source=self.task.source_dir,
            dest=self.task.dest_dir,
        )

        return TaskOutcome.COMPLETED if self._execute() else TaskOutcome.FAILED

    @abstractmethod
    def _execute(self) -> bool:
        """子类执行体；返回 False 表示本轮执行被致命中断（记 FAILED）。

        文件级错误不在此列——单文件失败由状态库计数机制兜底，任务完整
        跑完仍算 COMPLETED。停止指令导致的中断同样返回 False，此后任务
        循环会因停止检查整体退出，结果仅保持"中断 = 未完成"的一致性。
        """
        ...
