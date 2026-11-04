import glob
import logging
import os
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol, TextIO, override

if TYPE_CHECKING:
    from src.output.progress import ProgressReporter

SUCCESS_LEVEL_NUM = 25
logging.addLevelName(SUCCESS_LEVEL_NUM, "SUCCESS")


class Logger(Protocol):
    """应用日志接口（结构化协议）。

    生产实现为 :class:`LoggerWrapper`；测试替身（如 CaptureLogger）
    只需结构兼容即可注入，无需继承本协议。
    """

    def debug(self, msg: str, raw: bool = False) -> None: ...

    def info(self, msg: str, raw: bool = False, prepend_timestamp: bool = False) -> None: ...

    def success(self, msg: str, raw: bool = False) -> None: ...

    def warning(self, msg: str, raw: bool = False) -> None: ...

    def error(self, msg: str, raw: bool = False) -> None: ...

    def critical(self, msg: str, raw: bool = False) -> None: ...

    def progress_reporter(self) -> ProgressReporter | None:
        """创建 FFmpeg 进度的多路输出分发器；无可用输出设施时返回 None。"""
        ...


class DualFormatter(logging.Formatter):
    """
    自定义格式化器：
    如果日志记录中包含 'is_raw=True'，则只输出消息内容；
    否则输出带时间戳和等级的标准格式。
    """

    @override
    def format(self, record: logging.LogRecord) -> str:
        # 检查 record 中是否有 is_raw 属性且为 True
        if getattr(record, "is_raw", False):
            msg = record.getMessage()
            if getattr(record, "prepend_timestamp", False):
                # 格式化时间戳
                timestamp = self.formatTime(record, self.datefmt)
                return f"{timestamp} {msg}"
            return msg
        return super().format(record)


class _ProgressLineClearer(Protocol):
    """TTY 进度行的清行接口（由 src.output.progress 的控制台 sink 实现）。

    日志输出与 TTY 单行覆盖进度的协调契约：进度行在屏期间，日志 emit
    前先清行，避免日志与进度行黏连。
    """

    def clear_line(self) -> None: ...


class ConsoleLogHandler(logging.StreamHandler[TextIO]):
    """控制台 handler：存在活动 TTY 进度行时，先清行再输出日志记录。

    FFmpeg 进度在 TTY 上以 ``\\r`` 单行覆盖呈现（见 src.output.progress）；
    期间其他线程输出的日志若直接追加会与进度行黏连。emit 前回调进度写入
    方清行——handle() 已持有 handler 锁，清行与进度写入天然互斥，清行后
    的进度行由其下一轮刷新重绘。清行失败不阻断日志输出。
    """

    def __init__(self, stream: TextIO | None = None) -> None:
        super().__init__(stream)
        self.progress_writer: _ProgressLineClearer | None = None

    @override
    def emit(self, record: logging.LogRecord) -> None:
        writer = self.progress_writer
        if writer is not None:
            try:
                writer.clear_line()
            except Exception:  # noqa: BLE001, S110  清行失败不得影响日志本身，也无 logger 可用
                pass
        super().emit(record)


class LoggerWrapper(Logger):
    """
    Logger 的包装类，支持 raw 参数
    """

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def _sanitize(self, msg: str) -> str:
        if isinstance(msg, str):
            return msg.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
        return msg

    def debug(self, msg: str, raw: bool = False) -> None:
        """记录 debug 级别日志，raw=True 时不带格式"""
        self._logger.debug(self._sanitize(msg), extra={"is_raw": raw})

    def info(self, msg: str, raw: bool = False, prepend_timestamp: bool = False) -> None:
        """记录 info 级别日志，raw=True 时不带格式，prepend_timestamp=True 时在 raw 模式下也添加时间戳"""
        self._logger.info(
            self._sanitize(msg),
            extra={"is_raw": raw, "prepend_timestamp": prepend_timestamp},
        )

    def success(self, msg: str, raw: bool = False) -> None:
        """记录 success 级别日志，raw=True 时不带格式"""
        self._logger.log(SUCCESS_LEVEL_NUM, self._sanitize(msg), extra={"is_raw": raw})

    def warning(self, msg: str, raw: bool = False) -> None:
        """记录 warning 级别日志"""
        self._logger.warning(self._sanitize(msg), extra={"is_raw": raw})

    def error(self, msg: str, raw: bool = False) -> None:
        """记录 error 级别日志，raw=True 时不带格式"""
        self._logger.error(self._sanitize(msg), extra={"is_raw": raw})

    def critical(self, msg: str, raw: bool = False) -> None:
        """记录 critical 级别日志，raw=True 时不带格式"""
        self._logger.critical(self._sanitize(msg), extra={"is_raw": raw})

    # 如果需要访问底层 logger 的其他方法（如 warning, debug），可以通过 getattr 委托
    def __getattr__(self, name: str) -> Any:
        return getattr(self._logger, name)

    def progress_reporter(self) -> ProgressReporter | None:
        """创建 FFmpeg 进度分发器：从自身 handler 抽取日志文件与控制台流。"""
        # 延迟导入：progress.py 反向引用本模块的 handler 类型，避免运行期导入环
        from src.output.progress import reporter_from_handlers

        return reporter_from_handlers(self._logger.handlers)


class DailyRotatingFileHandler(logging.FileHandler):
    """
    按天滚动的日志处理器，每天生成一个新的日志文件，并清理旧日志。
    """

    def __init__(
        self,
        log_dir: str,
        max_log_files: int,
        prefix: str = "mediaconveyor",
        encoding: str = "utf-8",
    ) -> None:
        self.log_dir = log_dir
        self.max_log_files = max_log_files
        self.prefix = prefix
        self.current_date = datetime.now().strftime("%Y-%m-%d")
        filename = os.path.join(log_dir, f"{self.prefix}.{self.current_date}.log")
        super().__init__(filename, encoding=encoding)

    @override
    def emit(self, record: logging.LogRecord) -> None:
        new_date = datetime.now().strftime("%Y-%m-%d")
        if new_date != self.current_date:
            self.current_date = new_date
            self.close()
            filename = os.path.join(self.log_dir, f"{self.prefix}.{self.current_date}.log")
            self.baseFilename = os.path.abspath(filename)
            self.stream = self._open()

            # 日期变化时，执行旧日志清理
            if self.max_log_files > 0:
                clean_old_logs(self.log_dir, self.max_log_files, self.prefix)

        super().emit(record)


def setup_logger(
    log_dir: str,
    max_log_files: int = 0,
    log_level: str = "INFO",
    prefix: str = "mediaconveyor",
    console: bool = True,
    bridge_loggers: tuple[str, ...] = (),
) -> LoggerWrapper:
    """
    配置日志记录器，格式：2025-01-01 08:00:00 [INFO] 消息内容

    Args:
        console: 是否同时输出到控制台。脱离终端运行（start 子命令拉起的
            实例）没有可用的控制台，应传 False 仅写文件。
        bridge_loggers: 需要桥接到同一 handler 的业务模块 logger 名序列
            （如 ``src.fs``、``src.app.instance``），由组合根注入，
            本模块不感知业务模块的存在。
    """
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    logger = logging.getLogger(prefix)

    # 禁止向 root logger 传播，避免其他库配置的 handler 导致日志重复输出
    logger.propagate = False

    # 设置日志级别
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)
    logger.setLevel(numeric_level)

    # 清除旧的 handler 防止重复添加
    if logger.handlers:
        logger.handlers = []

    file_handler = DailyRotatingFileHandler(log_dir, max_log_files, prefix=prefix, encoding="utf-8")

    # 执行旧日志清理 (启动时清理一次)
    if max_log_files > 0:
        clean_old_logs(log_dir, max_log_files, prefix)

    # 定义标准格式字符串
    fmt_str = "%(asctime)s [%(levelname)s] %(message)s"

    # 使用自定义的 Formatter
    formatter = DualFormatter(fmt_str, datefmt="%Y-%m-%d %H:%M:%S")

    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # 同时输出到控制台（脱离终端运行的实例没有可用控制台，跳过）
    handlers: list[logging.Handler] = [file_handler]
    if console:
        console_handler = ConsoleLogHandler()
        console_handler.setFormatter(formatter)
        handlers.append(console_handler)

    for handler in handlers:
        logger.addHandler(handler)

    # 桥接业务模块日志：让统一文件操作与实例控制模块的日志输出到相同的 handler
    for name in bridge_loggers:
        bridged = logging.getLogger(name)
        bridged.setLevel(numeric_level)
        bridged.propagate = False
        for handler in handlers:
            bridged.addHandler(handler)

    # 返回包装后的 Logger 对象
    return LoggerWrapper(logger)


def clean_old_logs(log_dir: str, max_files: int, prefix: str = "mediaconveyor") -> None:
    """
    清理旧日志文件，只保留最近的 max_files 个
    """
    if max_files <= 0:
        return

    log_pattern = os.path.join(log_dir, f"{prefix}.*.log")

    # 获取所有匹配的日志文件
    files = glob.glob(log_pattern)

    # 按照文件名排序 (因为文件名包含 YYYY-MM-DD，所以按字母排序即按时间排序)
    # 从旧到新排序
    files.sort()

    # 如果文件数量超过限制
    if len(files) > max_files:
        # 计算需要删除的文件数量
        num_to_delete = len(files) - max_files
        # 获取最旧的那些文件
        files_to_delete = files[:num_to_delete]

        for f in files_to_delete:
            try:
                os.remove(f)
                print(f"已删除日志: {f}")
            except OSError as e:
                # 即使删除失败也不要在此时崩溃，仅打印错误
                print(f"删除日志失败: {f}\n{e}")
