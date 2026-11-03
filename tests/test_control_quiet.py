"""白盒测试：start --quiet / run-once --quiet 的确认信号与返回分支。

--quiet 语义 = 不进入日志跟随，确认信号后一行确认即返回：start 的
确认信号是实例锁被持有（_wait_instance_started 轮询），run-once 的
确认信号是受理回执（复用 _wait_run_once_accepted）。cmd_start 的
完整链路（真实分离式子进程）超出单元测试边界，_spawn_detached 属
进程边界允许打桩（tests/README.md 原则三）；实例存活用真实锁原语
模拟，状态文件用真实临时目录文件——二者正是判定的全部输入。
"""

import threading
import time
import uuid
from pathlib import Path

import pytest

from src.app import control
from src.app.instance import (
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


@pytest.fixture
def control_config(tmp_path, monkeypatch, unique_name):
    """把控制子命令的配置读取指向一次性实例标识的临时配置。

    cmd_start 走 strict 模式，仓库内 config.toml 已 gitignore 且不应
    被测试依赖——config_path 必须指向受控的临时文件。
    """
    config_file = tmp_path / "config.toml"
    config_file.write_text(f'instance_name = "{unique_name}"\n', encoding="utf-8")
    monkeypatch.setattr(control.paths, "config_path", lambda: str(config_file))


# ============================================================
# 启动确认等待（_wait_instance_started）
# ============================================================


def test_wait_instance_started_detects_lock(capture_log, unique_name):
    """锁被持有（实例存活）即确认启动成功。"""
    with SingleInstance(unique_name, capture_log):
        assert control._wait_instance_started(unique_name, timeout=1.0)


def test_wait_instance_started_times_out_without_instance(unique_name):
    """无实例持有锁时等待到超时，返回 False。"""
    started = time.monotonic()
    assert not control._wait_instance_started(unique_name, timeout=0.3)
    assert time.monotonic() - started >= 0.3


def test_wait_instance_started_reads_module_timeout_default(unique_name, monkeypatch):
    """timeout 缺省时每次调用读取模块常量——测试注入缩短等待的接线。"""
    monkeypatch.setattr(control, "_START_CONFIRM_TIMEOUT", 0.2)
    started = time.monotonic()
    assert not control._wait_instance_started(unique_name)
    elapsed = time.monotonic() - started
    assert 0.2 <= elapsed < 5.0


# ============================================================
# start --quiet
# ============================================================


def test_cmd_start_quiet_returns_after_lock_confirmed(
    monkeypatch, capture_log, unique_name, control_config, capsys
):
    """锁被持有即一行确认返回，不进入日志跟随。"""

    holder: list[SingleInstance] = []
    spawned: list[threading.Thread] = []

    def fake_spawn() -> int:
        def _acquire_later() -> None:
            time.sleep(0.2)  # 先让 cmd_start 的存活预检与首轮轮询落空
            inst = SingleInstance(unique_name, capture_log)
            inst.__enter__()
            holder.append(inst)

        t = threading.Thread(target=_acquire_later, daemon=True)
        t.start()
        spawned.append(t)
        return 4321

    monkeypatch.setattr(control, "_spawn_detached", fake_spawn)
    monkeypatch.setattr(control, "_START_CONFIRM_TIMEOUT", 5.0)

    control.cmd_start(quiet=True)

    # 等模拟子进程线程收尾，保证锁持有者已登记、可被可靠释放
    for t in spawned:
        t.join(timeout=5.0)
    assert holder, "模拟子进程未按预期持有实例锁"

    out = capsys.readouterr().out
    assert "实例已启动 (PID 4321)。" in out
    assert "滚动" not in out  # 不进入日志跟随

    for inst in holder:
        inst.__exit__(None, None, None)


def test_cmd_start_quiet_times_out_without_instance(
    monkeypatch, capture_log, unique_name, control_config, capsys
):
    """超时未确认（子进程未持锁）按失败退出，错误提示指向 log 子命令。"""
    monkeypatch.setattr(control, "_spawn_detached", lambda: 4321)
    monkeypatch.setattr(control, "_START_CONFIRM_TIMEOUT", 0.3)

    with pytest.raises(SystemExit) as exc_info:
        control.cmd_start(quiet=True)

    assert exc_info.value.code == 1
    assert "超时" in capsys.readouterr().err


# ============================================================
# run-once --quiet
# ============================================================


def test_cmd_run_once_quiet_returns_on_accept(
    monkeypatch, capture_log, unique_name, control_config, capsys
):
    """受理回执落盘即一行确认返回，不进入日志跟随。

    预写未来时间戳的回执：cmd 内部取的 sent_ts 必然不大于它，受理
    判定在首轮轮询即成立，不依赖真实等待。
    """
    monkeypatch.setattr(control, "_RUN_ONCE_ACCEPT_TIMEOUT", 1.0)

    with SingleInstance(unique_name, capture_log):
        write_state(unique_name, PHASE_SLEEPING, last_run_once=time.time() + 60)
        control.cmd_run_once(quiet=True)

    out = capsys.readouterr().out
    assert "指令已受理，实例即将执行一轮任务。" in out
    assert "滚动" not in out  # 不进入日志跟随


def test_cmd_run_once_quiet_exits_nonzero_when_ignored(
    monkeypatch, capture_log, unique_name, control_config, capsys
):
    """超时未受理（指令被竞态忽略）打印提示到 stderr 并按失败退出。"""
    monkeypatch.setattr(control, "_RUN_ONCE_ACCEPT_TIMEOUT", 0.3)

    with SingleInstance(unique_name, capture_log):
        write_state(unique_name, PHASE_SLEEPING)  # 休眠期但无任何回执
        with pytest.raises(SystemExit) as exc_info:
            control.cmd_run_once(quiet=True)

    assert exc_info.value.code == 1
    assert "未确认受理" in capsys.readouterr().err
