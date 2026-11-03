"""应用上下文：通过模块级单例解耦 Logger 等运行期单件的传递"""

from typing import TYPE_CHECKING

from src.app.lifecycle import LifecycleCoordinator

if TYPE_CHECKING:
    # 类型级依赖（运行时零导入）：core 包的 __init__ 会级联注册 handlers，
    # handlers.base 反向依赖本模块，运行时导入会造成循环
    from src.app.config import GlobalConfig
    from src.core.task_plan import TaskSpec
    from src.output.logger import Logger


class AppContext:
    _instance: AppContext | None = None

    @classmethod
    def init(
        cls,
        logger: Logger,
        config: GlobalConfig,
        tasks: list[TaskSpec] | None = None,
        instance_name: str = "",
    ) -> None:
        cls._instance = cls(logger, config, tasks, instance_name)

    @classmethod
    def get(cls) -> AppContext:
        if cls._instance is None:
            raise RuntimeError("AppContext 未初始化，请先调用 AppContext.init()")
        return cls._instance

    def __init__(
        self,
        logger: Logger,
        config: GlobalConfig,
        tasks: list[TaskSpec] | None = None,
        instance_name: str = "",
    ) -> None:
        self.logger = logger
        # 全局配置（加载期已校验的 GlobalConfig 模型）
        self.config = config
        # 加载期解析完成的任务编排信息（config=None 表示被拒绝的任务）
        self.tasks: list[TaskSpec] = tasks if tasks is not None else []
        # 实例标识名：锁文件 / 控制文件 / PID 文件 / 日志前缀的公共基名
        self.instance_name = instance_name
        # 生命周期协调器：ControlWatcher 注入停止与 run-once 指令，
        # 业务循环在边界查询
        self.lifecycle = LifecycleCoordinator()

    def update_config(self, config: GlobalConfig, tasks: list[TaskSpec]) -> None:
        self.config = config
        self.tasks = tasks
