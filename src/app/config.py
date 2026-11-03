"""统一配置管理器：加载、验证、重载、备份。

加载期完成所有"仅凭配置文件本身即可判定"的合法性校验（Pydantic 模型）：
- bootstrap 段（BootstrapConfig：log_dir / max_log_files / log_level），
  在日志系统就位前消费，失败走 print + exit 通道；
- 全局段（GlobalConfig：[schedule]）；
- 任务条目（core.task_plan.resolve_tasks：任务模型 + 交叉约束 + 依赖静态校验）。

校验失败分两类：全局/结构错误 → ConfigError（致命：启动期干净退出、
热重载回退备份）；单任务条目错误 → 记日志跳过该条目，不致命。
"""

import os
import shutil
import tomllib
from dataclasses import dataclass
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from src.core.task_plan import TaskSpec, resolve_tasks
from src.output.logger import Logger
from src.validation import format_validation_error


class ConfigError(Exception):
    """配置致命错误。"""


class ScheduleConfig(BaseModel):
    """[schedule] 配置块模型；仅调度路径消费（_run_schedule）。

    mode 不用 Literal：错误文案需逐字可控（Literal 的默认报错文案
    不满足），validator 抛出的 ValueError 经 format_validation_error
    原样透出。同理，interval_seconds 的"大于 0"约束由模型级校验
    承担而非字段级 gt，以保持错误文案统一。
    """

    mode: str | None = None
    cron_expr: str | None = None
    interval_seconds: float | None = None
    first_run_delay: float = -1

    @field_validator("mode")
    @classmethod
    def _validate_mode(cls, value: str | None) -> str | None:
        if value is not None and value not in ("cron", "interval"):
            raise ValueError(f"不支持的定时模式: {value}")
        return value

    @model_validator(mode="after")
    def _check_mode_requirements(self) -> Self:
        if self.mode == "cron" and not self.cron_expr:
            raise ValueError("cron 模式需要配置 cron_expr。")
        if self.mode == "interval" and (
            self.interval_seconds is None or self.interval_seconds <= 0
        ):
            raise ValueError("interval 模式需要配置有效的 interval_seconds (大于0的数字)。")
        return self


class GlobalConfig(BaseModel):
    """顶层全局配置段（未声明的配置键忽略）。

    数值约束在加载期收口——非法值致命（ConfigError）。
    """

    model_config = ConfigDict(extra="ignore")

    schedule: ScheduleConfig | None = None


class BootstrapConfig(BaseModel):
    """bootstrap 配置段：日志系统就位前消费的三项（其余键忽略）。

    校验先于日志系统就位，失败走 print + exit 通道（与"配置文件未找到"
    同通道）。instance_name 不在此列——其"非法回退默认 + 事后 warning"
    是有意的降级语义，且在更早的实例标识决策处解析。
    """

    model_config = ConfigDict(extra="ignore")

    log_dir: str = "./logs"
    max_log_files: int = Field(default=0, ge=0)
    log_level: str = "INFO"

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        """大小写不敏感（setup_logger 的既有行为），归一为大写。

        SUCCESS 为本应用注册的自定义级别；CRITICAL 为 logging 内建级别。
        """
        upper = value.upper()
        if upper not in ("DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"):
            raise ValueError(
                f"log_level 配置值无效: '{value}'，"
                "可选值: DEBUG、INFO、SUCCESS、WARNING、ERROR、CRITICAL"
            )
        return upper


@dataclass
class ConfigData:
    """从配置文件加载的完整数据集。"""

    config: GlobalConfig
    tasks: list[TaskSpec]
    used_backup: bool = False


class ConfigManager:
    """统一的配置生命周期管理器。

    启动时使用 ``load()``，运行时使用 ``reload()``（返回 None 表示无变化）。
    两条路径共享相同的加载、验证和错误处理逻辑。
    """

    def __init__(self, config_path: str, logger: Logger) -> None:
        self._config_path = os.path.abspath(config_path)
        self._backup_path = os.path.splitext(self._config_path)[0] + ".backup.toml"
        self._logger = logger
        self._last_mtime = self._get_mtime()

    # -- public API ----------------------------------------------------------

    def load(self, config: dict[str, Any] | None = None) -> ConfigData:
        """初始加载配置。

        若调用方已预先读取过配置文件，可传入 *config* 字典以避免重复读取。
        """
        if config is None:
            config = self.load_toml(self._config_path)
        return self._process(config, save_backup=True)

    def reload(self) -> ConfigData | None:
        """运行时重载配置。

        仅在文件 mtime 发生变化时执行；否则返回 ``None``。
        加载或验证失败时自动回退到备份配置。
        """
        if not self.has_changed():
            return None

        try:
            config = self.load_toml(self._config_path)
            return self._process(config, save_backup=True)
        except ConfigError as e:
            self._logger.warning(f"新配置文件验证失败: {e}，尝试加载备份配置……")
            self._last_mtime = self._get_mtime()
            return self._load_backup()

    def has_changed(self) -> bool:
        """配置文件 mtime 是否自上次加载后发生了变化。"""
        return self._get_mtime() != self._last_mtime

    # -- static helpers ------------------------------------------------------

    @staticmethod
    def load_toml(path: str) -> dict[str, Any]:
        """从路径读取 TOML 文件，返回字典。"""
        with open(path, "rb") as f:
            return tomllib.load(f)

    @staticmethod
    def _validate_structure(config: Any) -> None:
        """致命的结构验证——失败意味着配置文件不可用。"""
        if not isinstance(config, dict):
            raise ConfigError("配置文件内容不是有效的字典结构")

    # -- internal ------------------------------------------------------------

    def _process(self, config: dict[str, Any], save_backup: bool = False) -> ConfigData:
        """加载流程核心：验证 → 解析任务 → 保存备份 → 更新 mtime。"""
        self._validate_structure(config)

        try:
            root = GlobalConfig.model_validate(config)
        except ValidationError as e:
            raise ConfigError(f"全局配置无效：{format_validation_error(e)}") from e

        raw_tasks_value = config.get("tasks", [])
        raw_tasks: list[Any] = raw_tasks_value if isinstance(raw_tasks_value, list) else []
        if not isinstance(raw_tasks_value, list):
            self._logger.error("tasks 配置应为任务数组，已忽略全部任务。")
        tasks = resolve_tasks(raw_tasks, logger=self._logger)

        if save_backup:
            self._save_backup()

        self._last_mtime = self._get_mtime()
        self._logger.info("配置文件加载成功。")

        return ConfigData(config=root, tasks=tasks, used_backup=False)

    def _load_backup(self) -> ConfigData:
        """加载备份配置（致命失败会向上抛出）。"""
        try:
            config = self.load_toml(self._backup_path)
        except Exception as e:
            self._logger.error(f"备份配置文件加载失败: {e}")
            raise
        result = self._process(config, save_backup=False)
        result.used_backup = True
        return result

    def _save_backup(self) -> None:
        """将当前配置文件复制为备份。"""
        try:
            shutil.copy2(self._config_path, self._backup_path)
        except OSError as e:
            self._logger.warning(f"保存配置备份失败: {e}")

    def _get_mtime(self) -> float:
        try:
            return os.path.getmtime(self._config_path)
        except OSError:
            return -1
