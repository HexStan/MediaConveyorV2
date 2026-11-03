"""SIGTERM 停止信号的接线映射（白盒）。

为什么必须白盒：SIGTERM 的投递不可移植——Windows 的 ``os.kill`` 对
普通信号直接 TerminateProcess，无法在单元测试内向本进程投递
SIGTERM——只能同步调用已注册的处理器验证「信号 → fast 动作」的
映射契约；真实投递由 POSIX-only 的子进程测试单独覆盖（CI 的
POSIX 节点执行）。信号处理器是进程级全局状态，fixture 负责保存
并恢复原处理器，避免污染其他测试。
"""

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from src.app import instance, runtime
from src.app.lifecycle import install_sigterm_fast_stop

# 跨线程/跨进程同步的统一时限：只用于等待确定性事件，不构成时序依赖
_WAIT_TIMEOUT = 5.0

# 子进程脚本：安装处理器后常驻，SIGTERM 到达时写标记文件（进程边界测试）
_CHILD_CODE = """
import sys, time
sys.path.insert(0, sys.argv[1])
from src.app.lifecycle import install_sigterm_fast_stop


class _NullLogger:
    def warning(self, *args, **kwargs):
        pass


ready_path, marker_path = sys.argv[2], sys.argv[3]
install_sigterm_fast_stop(lambda: open(marker_path, "w").close(), _NullLogger())
open(ready_path, "w").close()
while True:
    time.sleep(0.1)
"""


@pytest.fixture
def restore_sigterm():
    """保存并恢复 SIGTERM 处理器（信号状态为进程级全局）。"""
    original = signal.getsignal(signal.SIGTERM)
    yield original
    signal.signal(signal.SIGTERM, original)


def test_sigterm_handler_dispatches_action_in_thread(restore_sigterm):
    """处理器只派生线程：模拟信号到达后，停止动作在子线程执行完毕。"""
    done = threading.Event()
    install_sigterm_fast_stop(done.set)

    handler = signal.getsignal(signal.SIGTERM)
    assert callable(handler)
    handler(signal.SIGTERM, None)  # 模拟信号到达：主线程同步调用处理器

    assert done.wait(timeout=_WAIT_TIMEOUT)


def test_install_off_main_thread_degrades_to_warning(restore_sigterm, capture_log):
    """非主线程安装只告警不抛错：装配环境异常不应中断启动。"""
    errors: list[BaseException] = []

    def _target() -> None:
        try:
            install_sigterm_fast_stop(lambda: None, capture_log)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    thread = threading.Thread(target=_target)
    thread.start()
    thread.join(timeout=_WAIT_TIMEOUT)

    assert errors == []
    assert capture_log.has("warning", "SIGTERM")
    # 安装失败不改动既有处理器
    assert signal.getsignal(signal.SIGTERM) is restore_sigterm


def test_stop_action_maps_three_modes(app_context):
    """停止模式到协调器请求的映射：graceful / slow / fast 各归其位。"""
    stop_action = runtime._make_stop_action(app_context)

    stop_action(instance.STOP_GRACEFUL)
    assert app_context.lifecycle.stop_after_file
    assert not app_context.lifecycle.stop_after_round
    assert not app_context.lifecycle.is_aborted()

    stop_action(instance.STOP_SLOW)
    assert app_context.lifecycle.stop_after_round
    assert not app_context.lifecycle.is_aborted()

    stop_action(instance.STOP_FAST)
    assert app_context.lifecycle.is_aborted()
    assert app_context.lifecycle.stop_requested()


def test_command_dispatcher_routes_stop_and_run_once(app_context):
    """控制指令分发：stop 各模式走停止动作；run-once 仅休眠期受理且不设停止标志。"""
    lifecycle = app_context.lifecycle
    stop_action = runtime._make_stop_action(app_context)
    dispatch = runtime._make_command_dispatcher(app_context, stop_action)

    # 默认 starting 相位：run-once 静默不受理
    dispatch(instance.CMD_RUN_ONCE, {"cmd": instance.CMD_RUN_ONCE, "ts": time.time()})
    assert not lifecycle.stop_requested()

    # 休眠期受理 run-once：只唤醒调度循环，不设置任何停止标志
    lifecycle.set_phase(instance.PHASE_SLEEPING)
    dispatch(instance.CMD_RUN_ONCE, {"cmd": instance.CMD_RUN_ONCE, "ts": time.time()})
    assert not lifecycle.stop_requested()

    # stop 各模式走停止动作
    dispatch(instance.CMD_STOP, {"mode": instance.STOP_GRACEFUL})
    assert lifecycle.stop_after_file


def test_sigterm_action_triggers_client_abort(app_context, restore_sigterm):
    """客户端装配：SIGTERM 处理器经由共享停止动作触发 fast 中止。"""
    stop_action = runtime._make_stop_action(app_context)
    install_sigterm_fast_stop(lambda: stop_action(instance.STOP_FAST), app_context.logger)

    handler = signal.getsignal(signal.SIGTERM)
    handler(signal.SIGTERM, None)

    deadline = time.monotonic() + _WAIT_TIMEOUT
    while not app_context.lifecycle.is_aborted() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert app_context.lifecycle.is_aborted()


@pytest.mark.skipif(os.name != "posix", reason="仅 POSIX 可向子进程投递真实 SIGTERM")
def test_real_sigterm_delivery(tmp_path):
    """真实信号投递（进程边界）：SIGTERM 触发停止动作且进程存活。

    Windows 的 ``os.kill`` 对普通信号直接 TerminateProcess，无法投递
    SIGTERM，故仅在 POSIX 执行（CI 的 POSIX 节点覆盖）。
    """
    ready = tmp_path / "ready"
    marker = tmp_path / "marker"
    root = Path(__file__).resolve().parents[1]
    proc = subprocess.Popen([sys.executable, "-c", _CHILD_CODE, str(root), str(ready), str(marker)])
    try:
        deadline = time.monotonic() + 10.0
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists(), "子进程未在时限内就绪（处理器安装失败？）"

        os.kill(proc.pid, signal.SIGTERM)

        deadline = time.monotonic() + _WAIT_TIMEOUT
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists(), "SIGTERM 未触发停止动作"
        assert proc.poll() is None, "SIGTERM 不应直接终止进程（应走 fast 停止流程）"
    finally:
        proc.kill()
        proc.wait(timeout=10.0)
