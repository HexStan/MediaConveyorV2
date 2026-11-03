"""测试公共设施。

分层策略与编写原则见 tests/README.md。本文件只提供三类设施：
- 捕获型日志替身（记录不拦截）；
- AppContext 装配（捕获日志，测试后清理单例）；
- run_task 灰盒入口（从 handler.execute() 驱动被测代码）。
"""

import pytest

from src.app.config import GlobalConfig
from src.app.context import AppContext
from src.core.registry import create_handler
from src.core.task_plan import resolve_tasks


class CaptureLogger:
    """捕获型日志替身：接口与 setup_logger 返回的 logger 对齐，
    只记录消息不产生任何输出，供测试断言日志行为。"""

    def __init__(self):
        self.messages = []  # [(level, message), ...]

    def _log(self, level, msg, **kwargs):
        self.messages.append((level, msg))

    def debug(self, msg, **kwargs):
        self._log("debug", msg, **kwargs)

    def info(self, msg, **kwargs):
        self._log("info", msg, **kwargs)

    def success(self, msg, **kwargs):
        self._log("success", msg, **kwargs)

    def warning(self, msg, **kwargs):
        self._log("warning", msg, **kwargs)

    def error(self, msg, **kwargs):
        self._log("error", msg, **kwargs)

    def critical(self, msg, **kwargs):
        self._log("critical", msg, **kwargs)

    def has(self, level, substring):
        """是否存在包含指定子串的某级别日志。"""
        return any(lv == level and substring in m for lv, m in self.messages)


@pytest.fixture
def capture_log():
    return CaptureLogger()


@pytest.fixture
def app_context(tmp_path, capture_log):
    """装配全局 AppContext：捕获日志，写入 tmp 目录，测试后清理单例。"""
    AppContext.init(capture_log, GlobalConfig())
    yield AppContext.get()
    AppContext._instance = None


@pytest.fixture
def run_task(app_context):
    """灰盒测试统一入口：以真实文件系统为环境执行一个任务。

    用法::

        handler = run_task({"mode": "transcode", "source_dir": ..., ...}, now=NOW)

    与生产装配路径对齐：任务字典先经加载期解析（resolve_tasks——任务
    模型校验，错误日志在此给出，被拒绝的任务返回 None），
    再经 create_handler 构造并执行。返回 handler 实例，可读取 stats 做
    补充断言。
    """

    def _run(task, now=None):
        ctx = AppContext.get()
        spec = resolve_tasks([task], logger=ctx.logger)[0]
        # 与生产 process_task 共用同一条装配路径，错误路径测试才是在测
        # 生产行为而不是测试替身。
        handler = create_handler(spec, now=now)
        if handler is None:
            return None
        handler.execute()
        return handler

    return _run
