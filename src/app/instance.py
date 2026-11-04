"""实例标识与跨进程控制通道。

「实例标识名」（instance_name）是同一份程序副本的身份证，锁文件、
控制文件、PID 文件、状态文件与日志前缀全部由它派生（前四者位于
系统临时目录）：

- ``<标识>.lock`` —— 实例锁（见本模块 ``SingleInstance``），
  同时充当存活探测依据：锁被占用即存在运行中的实例；
- ``<标识>.ctl`` —— 控制文件，``stop`` / ``run-once`` 子命令与
  运行实例之间的指令通道；
- ``<标识>.pid`` —— 运行实例的 PID，供 ``stop --fast`` 强杀兜底；
- ``<标识>.state`` —— 运行状态投影（调度相位与下次运行时间），
  供 ``run-once`` 子命令判定实例是否处于调度休眠期。

多副本部署时为每份程序配置不同的实例标识，锁、控制与日志天然隔离。
"""

import json
import logging
import os
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from types import TracebackType
from typing import Any, Self, TextIO, override

import psutil

from src.fs import atomic_writer
from src.output.logger import Logger

try:
    import fcntl
except ImportError:
    fcntl = None

try:
    # pyrefly 按 Linux 语义检查（见 pyproject.toml 的 [tool.pyrefly]），msvcrt 是 Windows 专属
    # 模块，其属性在 Linux 语义下不可见；此处运行时由 ImportError 守卫
    import msvcrt
except ImportError:
    msvcrt = None

logger = logging.getLogger(__name__)

# stop 指令的三种模式
STOP_GRACEFUL = "graceful"  # 当前文件传输完成后停止
STOP_SLOW = "slow"  # 本轮任务结束后停止
STOP_FAST = "fast"  # 立即中止传输并退出

_STOP_MODES = (STOP_GRACEFUL, STOP_SLOW, STOP_FAST)

# 控制文件指令格式（JSON）：
# {"cmd": "stop", "mode": ..., "ts": ...} / {"cmd": "run-once", "ts": ...}
CMD_STOP = "stop"
CMD_RUN_ONCE = "run-once"
_CMDS = (CMD_STOP, CMD_RUN_ONCE)

# 实例调度相位：LifecycleCoordinator 的内存值是权威源，状态文件是
# 供跨进程读取的投影。仅 sleeping 相位受理 run-once 指令。
PHASE_STARTING = "starting"  # 已获锁、尚未进入调度循环
PHASE_SLEEPING = "sleeping"  # 等待下个调度时间点
PHASE_RUNNING = "running"  # 正在执行一轮任务

PHASES = (PHASE_STARTING, PHASE_SLEEPING, PHASE_RUNNING)

# start 子命令标记分离式子进程的环境变量：子进程据此关闭控制台输出
DETACHED_ENV = "MEDIACONVEYOR_DETACHED"


# ============================================================
# 单实例 / 文件锁
# ============================================================

# instance_name 未配置或非法时使用的默认实例标识
DEFAULT_INSTANCE_NAME = "mediaconveyor"


def _lock_available() -> bool:
    """当前平台是否具备文件锁原语（POSIX 的 fcntl 或 Windows 的 msvcrt）。"""
    return fcntl is not None or msvcrt is not None


def _acquire_lock(fp: TextIO) -> None:
    """在已打开的文件对象上加非阻塞排他锁，锁被占用时抛出 OSError。"""
    if fcntl is not None:
        # LOCK_EX: 排他锁, LOCK_NB: 非阻塞
        fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    elif msvcrt is not None:
        # Windows 的字节范围锁作用于当前位置起的若干字节，
        # 对空文件的首字节加锁同样有效
        fp.seek(0)
        msvcrt.locking(fp.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[pyrefly:missing-attribute]


def _release_lock(fp: TextIO) -> None:
    """释放 _acquire_lock 加上的锁（Windows 必须对同一字节区间解锁）。"""
    if fcntl is not None:
        fcntl.flock(fp, fcntl.LOCK_UN)
    elif msvcrt is not None:
        fp.seek(0)
        msvcrt.locking(fp.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[pyrefly:missing-attribute]


def resolve_instance_name(value: object, default_name: str) -> tuple[str, bool]:
    """把 instance_name 配置值解析为实例标识，返回 (标识, 是否非法回退)。

    配置值只接受纯名称（不含任何路径成分），锁文件、控制文件、PID
    文件的存放目录由程序按平台决定，用户不可自定义。未配置时静默
    使用默认名；配置了但非法时回退默认名并标记非法，由调用方给出警告。
    """
    if isinstance(value, str):
        name = value.strip()
        if name and name not in (".", "..") and name == os.path.basename(name):
            return name, False
        if name:
            return default_name, True
    return default_name, False


def resolve_identity(raw_config: dict[str, Any]) -> tuple[str, bool]:
    """从原始配置解析实例标识，返回 (标识, 是否非法回退默认)。"""
    return resolve_instance_name(raw_config.get("instance_name"), DEFAULT_INSTANCE_NAME)


class InstancePaths:
    """实例运行时文件的路径集合（锁 / 控制 / PID / 状态，均在系统临时目录）。"""

    def __init__(self, name: str) -> None:
        self.name: str = name
        self.directory: str = tempfile.gettempdir()

    @property
    def lock(self) -> str:
        return os.path.join(self.directory, f"{self.name}.lock")

    @property
    def ctl(self) -> str:
        return os.path.join(self.directory, f"{self.name}.ctl")

    @property
    def pid(self) -> str:
        return os.path.join(self.directory, f"{self.name}.pid")

    @property
    def state(self) -> str:
        return os.path.join(self.directory, f"{self.name}.state")


def instance_paths(instance_name: str) -> InstancePaths:
    return InstancePaths(instance_name)


class SingleInstance:
    """
    文件锁上下文管理器，防止多实例运行

    - Linux / macOS：fcntl.flock
    - Windows：msvcrt.locking（锁住文件首字节）
    - 锁由操作系统在进程退出时自动释放，不会残留死锁
    - 获锁后写入 PID 文件（供 ``stop --fast`` 强杀兜底）；退出时清理
      PID、控制与状态文件——持锁期间它们必然属于本实例
    """

    def __init__(self, instance_name: str, logger: Logger) -> None:
        self.instance_name = instance_name
        self.paths = instance_paths(instance_name)
        self.logger = logger
        self.fp: TextIO | None = None

    def __enter__(self) -> Self:
        if not _lock_available():
            self.logger.warning("当前平台没有可用的文件锁原语，实例锁未启用。")
            return self

        try:
            self.fp = open(self.paths.lock, "w")
        except Exception as e:  # noqa: BLE001
            self.logger.error(f"无法创建锁文件 {self.paths.lock}: {e}")
            sys.exit(1)

        try:
            _acquire_lock(self.fp)
        except OSError:
            self.logger.info("另一个实例被启动了，现已自动退出。")
            sys.exit(1)
        except Exception as e:  # noqa: BLE001
            self.logger.error(f"无法获取实例锁: {e}")
            sys.exit(1)

        self._write_pid_file()
        return self

    def _write_pid_file(self) -> None:
        """写入本进程 PID，供 stop --fast 超时强杀定位目标。"""
        try:
            with open(self.paths.pid, "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
        except OSError as e:
            self.logger.warning(f"写入 PID 文件失败（stop --fast 将无法强杀兜底）: {e}")

    def _cleanup_runtime_files(self) -> None:
        """清理 PID、控制与状态文件（必须在释放锁之前执行）。"""
        for path in (self.paths.pid, self.paths.ctl, self.paths.state):
            try:
                os.remove(path)
            except OSError:
                pass

    def __exit__(
        self,
        _type: type[BaseException] | None,
        value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self.fp is None:
            return
        self._cleanup_runtime_files()
        try:
            _release_lock(self.fp)
        except Exception:  # noqa: BLE001, S110
            pass
        finally:
            try:
                self.fp.close()
            except Exception:  # noqa: BLE001, S110
                pass
            self.fp = None


def is_running(instance_name: str) -> bool:
    """探测是否存在持有实例锁的运行实例。

    以「能否拿到锁」为准而不是检查 PID：锁由操作系统在进程退出
    （含崩溃被杀）时自动释放，不存在 PID 复用误判。
    """
    lock_path = instance_paths(instance_name).lock
    if not os.path.exists(lock_path):
        return False
    try:
        with open(lock_path, "a+") as fp:
            try:
                _acquire_lock(fp)
            except OSError:
                return True  # 锁被占用 → 实例存活
            _release_lock(fp)
            return False  # 锁空闲 → 无实例（仅剩崩溃残留的锁文件）
    except OSError:
        return False


def read_pid(instance_name: str) -> int | None:
    """读取运行实例的 PID；文件缺失或内容非法时返回 None。"""
    try:
        with open(instance_paths(instance_name).pid, encoding="utf-8") as f:
            return int(f.read().strip())
    except OSError, ValueError:
        return None


def send_stop(instance_name: str, mode: str) -> None:
    """原子写入停止指令到控制文件。"""
    if mode not in _STOP_MODES:
        raise ValueError(f"未知的停止模式: {mode}")
    payload = json.dumps({"cmd": CMD_STOP, "mode": mode, "ts": time.time()})
    with atomic_writer(instance_paths(instance_name).ctl) as f:
        f.write(payload.encode("utf-8"))


def send_run_once(instance_name: str) -> None:
    """原子写入立即执行一轮任务的指令到控制文件。"""
    payload = json.dumps({"cmd": CMD_RUN_ONCE, "ts": time.time()})
    with atomic_writer(instance_paths(instance_name).ctl) as f:
        f.write(payload.encode("utf-8"))


def write_state(
    instance_name: str,
    phase: str,
    next_run: float | None = None,
    last_run_once: float | None = None,
) -> None:
    """原子写入实例运行状态文件（best-effort：失败仅记日志）。

    调度相位以 LifecycleCoordinator 的内存值为权威源，状态文件是供
    ``run-once`` 子命令跨进程读取的投影；``last_run_once`` 是最近一次
    受理的 run-once 指令时间戳（受理回执，供发送方确认指令已生效）。
    """
    if phase not in PHASES:
        raise ValueError(f"未知的实例相位: {phase}")
    payload = json.dumps(
        {
            "phase": phase,
            "next_run": next_run,
            "last_run_once": last_run_once,
            "ts": time.time(),
        }
    )
    try:
        with atomic_writer(instance_paths(instance_name).state) as f:
            f.write(payload.encode("utf-8"))
    except OSError as e:
        logger.warning(f"写入状态文件失败（run-once 将无法判定休眠期）: {e}")


def read_state(instance_name: str) -> dict[str, Any] | None:
    """读取实例运行状态；文件缺失、内容损坏或相位非法时返回 None。"""
    try:
        with open(instance_paths(instance_name).state, encoding="utf-8") as f:
            payload = json.load(f)
    except OSError, ValueError:
        return None
    if not isinstance(payload, dict) or payload.get("phase") not in PHASES:
        return None
    return payload


def wait_for_exit(
    instance_name: str,
    timeout: float | None = None,
    poll_interval: float = 0.5,
    on_wait: Callable[[float], None] | None = None,
) -> bool:
    """阻塞等待实例退出（基于锁探测轮询），返回 True 表示已退出。

    Args:
        timeout: 最长等待秒数；None 表示一直等待。
        on_wait: 每次轮询调用的回调 ``on_wait(elapsed)``，供展示等待进度。
    """
    started = time.time()
    while True:
        if not is_running(instance_name):
            return True
        elapsed = time.time() - started
        if timeout is not None and elapsed >= timeout:
            return False
        if on_wait is not None:
            on_wait(elapsed)
        time.sleep(poll_interval)


def force_kill_tree(pid: int, term_timeout: float = 3.0) -> bool:
    """强制结束进程及其全部后代（``stop --fast`` 的兜底手段）。

    动手前 best-effort 校验目标进程的可执行文件名与当前程序一致，
    防止 PID 文件残留遇到 PID 复用时误杀无关进程。

    Returns:
        False 表示校验未通过，未执行任何终止操作。
    """
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return True
    except psutil.Error:
        return False

    if not _pid_belongs_to_us(parent):
        return False

    try:
        targets = [parent, *parent.children(recursive=True)]
    except psutil.Error:
        targets = [parent]
    _terminate_and_kill(targets, term_timeout)
    return True


def _pid_belongs_to_us(process: psutil.Process) -> bool:
    """best-effort 校验目标进程是本程序（可执行文件名一致）。"""
    try:
        exe = process.exe()
    except psutil.Error:
        return True  # 无法检查时放行：锁被占用已提供主要置信
    if not exe:
        return True
    expected = os.path.basename(sys.executable).lower()
    return os.path.basename(exe).lower() == expected


def _terminate_and_kill(targets: list[psutil.Process], term_timeout: float) -> None:
    """先 terminate 后 kill：给进程留出优雅退出的窗口，超时者强杀。"""
    for proc in targets:
        try:
            proc.terminate()
        except psutil.Error:
            pass
    _gone, alive = psutil.wait_procs(targets, timeout=term_timeout)
    for proc in alive:
        try:
            proc.kill()
        except psutil.Error:
            pass


def terminate_process_tree(
    pid: int, term_timeout: float = 3.0, logger: Logger | None = None
) -> None:
    """终止进程树，不留孤儿进程。

    供 ``LifecycleCoordinator`` 在 fast 中止时终止外部同步进程
    （FFmpeg 等外部工具可能派生子进程，只杀直接子进程会留下孤儿）。
    """
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    except psutil.Error as e:
        if logger:
            logger.warning(f"终止外部进程 (PID {pid}) 失败: {e}")
        return

    try:
        targets = [parent, *parent.children(recursive=True)]
    except psutil.Error:
        targets = [parent]
    _terminate_and_kill(targets, term_timeout)


class ControlWatcher(threading.Thread):
    """控制文件监视线程（守护线程）：轮询 ctl 指令并分发。

    实例获锁后启动；读到合法指令（stop / run-once）即删除控制文件，
    以 ``on_command(cmd, payload)`` 分发（payload 为原始 JSON 字典，
    指令合法性已由本类校验）。实例启动之前写入的遗留指令（上次实例
    崩溃残留）会被忽略，避免误停新实例。
    """

    def __init__(
        self,
        instance_name: str,
        on_command: Callable[[str, dict[str, Any]], None],
        poll_interval: float = 0.5,
    ) -> None:
        super().__init__(daemon=True, name=f"control-watcher-{instance_name}")
        self._ctl_path = instance_paths(instance_name).ctl
        self._on_command = on_command
        self._poll_interval = poll_interval
        self._started_at = time.time()

    @override
    def run(self) -> None:
        while True:
            try:
                self.poll_once()
            except Exception as e:  # noqa: BLE001
                # 回调异常绝不能让监视线程死掉，否则实例将无法被 stop 控制
                logger.error(f"处理控制指令时发生错误: {e}")
            time.sleep(self._poll_interval)

    def poll_once(self) -> None:
        """单次轮询：读取、校验、删除控制文件并分发指令。"""
        try:
            with open(self._ctl_path, encoding="utf-8") as f:
                payload = json.load(f)
        except OSError, ValueError:
            return  # 不存在、写入未完成或内容损坏，下轮再试
        if not isinstance(payload, dict):
            return

        cmd = payload.get("cmd")
        if cmd not in _CMDS:
            return
        if cmd == CMD_STOP and payload.get("mode") not in _STOP_MODES:
            return

        try:
            ts = float(payload.get("ts", 0))
        except TypeError, ValueError:
            return
        if ts < self._started_at:
            return  # 上个实例的遗留指令，忽略

        try:
            os.remove(self._ctl_path)
        except OSError:
            return

        self._on_command(cmd, payload)
