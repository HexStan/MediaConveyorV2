"""白盒测试：run-once 子命令的前置判定与受理探测。

cmd_run_once 的完整链路（分离式实例、终端日志跟随）需要真实后台进程
与交互终端，超出单元测试边界；此处直接驱动前置判定函数
（_run_once_blocker）与受理回执等待循环（_wait_run_once_accepted）。
实例存活用真实锁原语模拟（同进程持有 SingleInstance），状态文件用
真实临时目录文件——二者正是判定的全部输入，不引入业务打桩
（tests/README.md 原则三）。
"""

import time
import uuid
from pathlib import Path

import pytest

from src.app import control
from src.app.instance import (
    PHASE_RUNNING,
    PHASE_SLEEPING,
    SingleInstance,
    instance_paths,
    write_state,
)


@pytest.fixture
def unique_name():
    """全局临时目录里的一次性实例标识，测试结束自动清理运行时文件。"""
    name = f"mediaconveyor-test-{uuid.uuid4().hex}"
    yield name
    paths = instance_paths(name)
    for path in (paths.lock, paths.pid, paths.ctl, paths.state):
        Path(path).unlink(missing_ok=True)


# ============================================================
# 前置判定（_run_once_blocker）
# ============================================================


def test_blocker_rejects_when_no_instance_running(unique_name):
    assert control._run_once_blocker(unique_name) == "没有正在运行的实例。"


def test_blocker_rejects_when_state_unreadable(capture_log, unique_name):
    """实例存活但状态文件缺失（如写入失败）：拒绝并说明原因。"""
    with SingleInstance(unique_name, capture_log):
        assert control._run_once_blocker(unique_name) == (
            "无法读取实例运行状态，无法确认实例是否处于调度休眠期。"
        )


def test_blocker_rejects_when_not_sleeping(capture_log, unique_name):
    with SingleInstance(unique_name, capture_log):
        write_state(unique_name, PHASE_RUNNING)
        assert "不在调度休眠期" in control._run_once_blocker(unique_name)


def test_blocker_allows_sleeping_instance(capture_log, unique_name):
    with SingleInstance(unique_name, capture_log):
        write_state(unique_name, PHASE_SLEEPING)
        assert control._run_once_blocker(unique_name) is None


# ============================================================
# 受理回执等待（_wait_run_once_accepted）
# ============================================================


def test_wait_run_once_accepted_detects_ack(capture_log, unique_name):
    """状态文件携带 >= 发送时刻的受理回执即视为受理（无瞬时窗口）。"""
    sent_ts = time.time() - 5
    with SingleInstance(unique_name, capture_log):
        write_state(unique_name, PHASE_RUNNING, last_run_once=sent_ts + 1)
        assert control._wait_run_once_accepted(unique_name, sent_ts, timeout=1.0)


def test_wait_run_once_accepted_ignores_stale_ack(capture_log, unique_name):
    """回执早于本次发送时刻（上一条指令的回执）不得误判为受理。"""
    stale_ts = time.time() - 10
    with SingleInstance(unique_name, capture_log):
        write_state(unique_name, PHASE_SLEEPING, last_run_once=stale_ts)
        assert not control._wait_run_once_accepted(unique_name, stale_ts + 1, timeout=0.3)


def test_wait_run_once_accepted_times_out_without_ack(capture_log, unique_name):
    with SingleInstance(unique_name, capture_log):
        write_state(unique_name, PHASE_SLEEPING)
        started = time.monotonic()
        assert not control._wait_run_once_accepted(unique_name, time.time(), timeout=0.3)
        assert time.monotonic() - started >= 0.3


def test_wait_run_once_accepted_stops_when_instance_exits(unique_name):
    """实例中途退出立即终止等待，不等满时限。"""
    assert not control._wait_run_once_accepted(unique_name, time.time(), timeout=1.0)
