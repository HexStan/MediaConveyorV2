"""实例锁测试：跨进程互斥契约 + instance_name 解析契约。

单实例锁的本质是进程间互斥——只有当第二个真实进程拿不到锁时，"防止多实例"
才成立。因此锁行为以子进程为边界驱动真实锁原语（原则三：桩只放在进程边界，
这里连桩都不需要，子进程就是真实世界）。Linux/macOS 走 fcntl.flock，
Windows 走 msvcrt.locking，CI 矩阵天然覆盖两种实现。

名称解析（resolve_instance_name）是纯函数。它的"未填/非法 → 回退默认名"
契约若经由 SingleInstance 验证，会在系统临时目录真实创建默认名锁文件，
可能与维护者正在运行的实例互斥，因此直接对纯函数断言（白盒补盲）。
"""

import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from src.app.instance import (
    DEFAULT_INSTANCE_NAME,
    SingleInstance,
    resolve_instance_name,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 子进程脚本：以命令行参数为 instance_name 配置值尝试获取实例锁。
# 拿到锁 → 打印 locked 并正常退出（0）；锁被占用 → sys.exit(1)（退出码 1）。
CHILD_CODE = """
import sys

from src.app.instance import SingleInstance


class QuietLogger:
    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        pass


with SingleInstance(sys.argv[1], QuietLogger()):
    print("locked")
"""


def _unique_instance_name():
    """系统临时目录是全局共享的，用随机名避免与其他进程/测试互扰。"""
    return f"mediaconveyor-test-{uuid.uuid4().hex}"


def _lock_path(instance_name):
    return Path(tempfile.gettempdir()) / f"{instance_name}.lock"


def _acquire_in_child(instance_name):
    return subprocess.run(
        [sys.executable, "-c", CHILD_CODE, instance_name],
        check=False,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_second_process_cannot_acquire_held_lock(capture_log):
    """第一个实例持锁期间，第二个进程必须拿不到锁并以退出码 1 退出。"""
    name = _unique_instance_name()

    try:
        with SingleInstance(name, capture_log):
            result = _acquire_in_child(name)
    finally:
        _lock_path(name).unlink(missing_ok=True)

    assert result.returncode == 1
    assert "locked" not in result.stdout


def test_lock_released_after_context_exit(capture_log):
    """with 块结束后锁被释放，后续进程可以正常获取。"""
    name = _unique_instance_name()

    try:
        with SingleInstance(name, capture_log):
            pass
        result = _acquire_in_child(name)
    finally:
        _lock_path(name).unlink(missing_ok=True)

    assert result.returncode == 0
    assert "locked" in result.stdout


def test_lock_file_located_in_platform_temp_dir(capture_log):
    """锁文件位置由程序决定：统一存放在系统临时目录，配置值仅作为文件名。"""
    name = _unique_instance_name()

    try:
        with SingleInstance(name, capture_log):
            assert _lock_path(name).exists()
    finally:
        _lock_path(name).unlink(missing_ok=True)


def test_pid_file_written_while_holding_lock_and_removed_on_exit(capture_log):
    """持锁期间写入 PID 文件供强杀兜底，退出时清理。"""
    name = _unique_instance_name()
    pid_path = Path(tempfile.gettempdir()) / f"{name}.pid"
    lock_path = _lock_path(name)

    try:
        with SingleInstance(name, capture_log):
            assert pid_path.read_text(encoding="utf-8") == str(os.getpid())
        assert not pid_path.exists()
    finally:
        pid_path.unlink(missing_ok=True)
        lock_path.unlink(missing_ok=True)


def test_control_file_removed_on_exit(capture_log):
    """持锁期间残留的控制文件（实例崩溃前未处理的指令）在退出时清理。"""
    name = _unique_instance_name()
    ctl_path = Path(tempfile.gettempdir()) / f"{name}.ctl"
    lock_path = _lock_path(name)

    try:
        with SingleInstance(name, capture_log):
            ctl_path.write_text("{}", encoding="utf-8")
        assert not ctl_path.exists()
    finally:
        ctl_path.unlink(missing_ok=True)
        lock_path.unlink(missing_ok=True)


def test_valid_instance_name_is_kept():
    """合法纯名称原样生效。"""
    assert resolve_instance_name("my-archive", DEFAULT_INSTANCE_NAME) == (
        "my-archive",
        False,
    )


def test_missing_instance_name_uses_default():
    """未配置 instance_name 时静默使用默认标识。"""
    assert resolve_instance_name(None, DEFAULT_INSTANCE_NAME) == (
        DEFAULT_INSTANCE_NAME,
        False,
    )


def test_blank_instance_name_uses_default():
    """空白值视为未配置。"""
    assert resolve_instance_name("", DEFAULT_INSTANCE_NAME) == (
        DEFAULT_INSTANCE_NAME,
        False,
    )
    assert resolve_instance_name("   ", DEFAULT_INSTANCE_NAME) == (
        DEFAULT_INSTANCE_NAME,
        False,
    )


def test_path_style_instance_name_falls_back_to_default():
    """路径写法非法：回退默认标识并标记。"""
    assert resolve_instance_name("/tmp/smartarchiver", DEFAULT_INSTANCE_NAME) == (
        DEFAULT_INSTANCE_NAME,
        True,
    )


def test_relative_path_instance_name_falls_back_to_default():
    """带子目录成分的名字同样非法。"""
    assert resolve_instance_name("sub/dir", DEFAULT_INSTANCE_NAME) == (
        DEFAULT_INSTANCE_NAME,
        True,
    )


def test_dot_instance_name_falls_back_to_default():
    """当前目录 / 上级目录这类特殊名非法。"""
    assert resolve_instance_name(".", DEFAULT_INSTANCE_NAME) == (
        DEFAULT_INSTANCE_NAME,
        True,
    )
    assert resolve_instance_name("..", DEFAULT_INSTANCE_NAME) == (
        DEFAULT_INSTANCE_NAME,
        True,
    )
