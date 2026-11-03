"""测试公共设施。

分层策略与编写原则见 tests/README.md。本文件只提供三类设施：
- 捕获型日志替身（记录不拦截）；
- AppContext 装配（真实 StateManager + 捕获日志，测试后清理单例）；
- run_task 灰盒入口（从 handler.execute() 驱动被测代码）。
"""

import io
import subprocess

import pytest

from src.app.config import GlobalConfig
from src.app.context import AppContext
from src.core.handlers import create_handler
from src.core.media import ffmpeg as ffmpeg_module
from src.core.state import StateManager
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
    """装配全局 AppContext：捕获日志 + 真实 StateManager（写入 tmp 目录）。"""
    state = StateManager(str(tmp_path / "state.db"))
    AppContext.init(capture_log, GlobalConfig(), state=state)
    yield AppContext.get()
    state.close()
    AppContext._instance = None


@pytest.fixture(autouse=True)
def _reset_handler_class_state():
    """清理处理器的类级提示去重状态（源目录告警 / 监听提示），保证测试互不污染。"""
    from src.core.handlers.ffmpeg_task import FFmpegTaskHandler

    FFmpegTaskHandler._warned_dirs.clear()
    FFmpegTaskHandler._monitoring_logged.clear()
    yield
    FFmpegTaskHandler._warned_dirs.clear()
    FFmpegTaskHandler._monitoring_logged.clear()


class FakePopen:
    """模拟 ffmpeg 子进程：stderr 按 chunk 输出，退出码可配置。"""

    def __init__(self, returncode=0, output=b"", hangs=False):
        self.stderr = io.BytesIO(output)
        self.returncode = None if hangs else returncode
        self.pid = 4321
        self._hangs = hangs
        self.waited = False

    def poll(self):
        return None if self._hangs else self.returncode

    def wait(self, timeout=None):
        self.waited = True
        if self._hangs:
            # 现实中 terminate 后进程退出；测试里以退出码收尾
            self.returncode = self.returncode or -9
        return self.returncode


@pytest.fixture
def stub_ffmpeg(monkeypatch):
    """进程边界打桩（tests/README.md 原则三）：记录传给 ffmpeg 的命令行。

    - ``calls``：收到的完整命令行列表；
    - ``state``：可配置的返回行为（returncode / output / hangs / on_start，
      on_start 在子进程"启动"时调用，可用来模拟 ffmpeg 写出半成品文件）。
    """
    calls: list[str] = []
    state = {"returncode": 0, "output": b"", "on_start": None, "hangs": False}

    def fake_popen(cmd, **kwargs):
        calls.append(cmd)
        if state["on_start"] is not None:
            state["on_start"](cmd)
        return FakePopen(
            returncode=state["returncode"],
            output=state["output"],
            hangs=state["hangs"],
        )

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(ffmpeg_module, "_POLL_INTERVAL", 0.05)
    # 超时终止走真实协调器 → 进程树终止打桩为无副作用
    monkeypatch.setattr("src.app.lifecycle.terminate_process_tree", lambda pid, logger=None: True)
    return calls, state


@pytest.fixture
def run_task(app_context):
    """灰盒测试统一入口：以真实文件系统为环境执行一个任务。

    用法::

        handler = run_task({"source_dir": ..., "dest_dir": ..., ...}, now=NOW)

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
