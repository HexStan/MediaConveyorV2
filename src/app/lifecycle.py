"""停止指令与调度唤醒的协调器。

运行实例收到停止指令后的行为由三级信号驱动：

- graceful（``stop`` 默认）——当前文件传输完成后停止；
- slow（``stop --slow``）——本轮任务结束后停止；
- fast（``stop --fast``）——立即中止文件传输、丢弃当前任务，
  清理已写入的临时文件后退出。

信号由 ``src.app.instance.ControlWatcher`` 从控制文件读入后注入，Docker /
systemd 停服的 SIGTERM 由 ``install_sigterm_fast_stop`` 接入同一条 fast 链路；
业务循环在文件/任务边界查询，``fs.transfer`` 的复制循环在 chunk 边界查询
fast 中止。sync 模式的外部同步进程在启动时登记，fast 中止时被整树终止，
不留孤儿。

协调器同时维护实例调度相位（``PHASE_*``，进程内权威源）：``run-once``
指令仅在 ``sleeping`` 相位受理——受理后设置唤醒事件打断调度睡眠，
下一轮任务立即执行；其余相位到达的指令静默忽略，由 ``run-once``
终端经状态文件预检与受理探测反馈。
"""

import signal
import subprocess
import threading
from collections.abc import Callable
from typing import Any

from src import fs
from src.app.instance import PHASE_SLEEPING, PHASE_STARTING, PHASES, terminate_process_tree
from src.output.logger import Logger


class LifecycleCoordinator:
    """实例生命周期信号的汇聚点与外部同步进程的登记处。

    每个运行实例持有唯一实例（经 AppContext 分发）：``ControlWatcher``
    线程负责注入停止与 run-once 指令，调度相位由调度循环更新，
    业务代码只读状态。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop_after_file = False
        self._stop_after_round = False
        self._abort_event = threading.Event()
        # 任何停止指令或受理的 run-once 都会触发，用于打断调度循环的长睡眠
        self._wake_event = threading.Event()
        # 实例调度相位（run-once 受理判定依据；状态文件是其跨进程投影）
        self._phase = PHASE_STARTING
        # 最近一次受理的 run-once 指令时间戳（受理回执，随状态文件持久化）
        self._run_once_accepted_ts: float | None = None
        # 外部子进程（二进制管道）：协调器只消费 .pid，泛型参数不限定编解码形态
        self._children: list[subprocess.Popen[Any]] = []

    # ---- 调度相位（调度循环更新） ------------------------------------

    def set_phase(self, phase: str) -> None:
        """更新实例调度相位。"""
        if phase not in PHASES:
            raise ValueError(f"未知的实例相位: {phase}")
        with self._lock:
            self._phase = phase

    @property
    def phase(self) -> str:
        """当前调度相位。"""
        with self._lock:
            return self._phase

    @property
    def run_once_accepted_ts(self) -> float | None:
        """最近一次受理的 run-once 指令时间戳；从未受理过则为 None。"""
        with self._lock:
            return self._run_once_accepted_ts

    # ---- 指令注入（ControlWatcher 调用） ----------------------------

    def request_stop_after_file(self) -> None:
        """graceful：当前文件传输完后停止。"""
        self._stop_after_file = True
        self._wake_event.set()

    def request_stop_after_round(self) -> None:
        """slow：本轮任务结束后停止。"""
        self._stop_after_round = True
        self._wake_event.set()

    def request_abort(self, logger: Logger | None = None) -> None:
        """fast：立即中止传输，并终止已登记的外部同步进程树。"""
        self._abort_event.set()
        self._wake_event.set()
        self.terminate_children(logger)

    def request_run_now(self, ts: float) -> bool:
        """run-once：仅当实例处于调度休眠期时受理并唤醒调度循环。

        受理时记账指令时间戳 ``ts``（此后每次状态写入都携带该回执，
        供发送方以 ``last_run_once >= ts`` 确认指令已生效——执行轮次
        可能短于轮询间隔，瞬时相位无法作为旁证）。返回是否受理；
        非休眠相位（正在执行任务、尚未进入调度循环）到达的指令静默
        忽略——反馈由 ``run-once`` 终端负责，实例日志不记录忽略事件。
        """
        with self._lock:
            if self._phase != PHASE_SLEEPING:
                return False
            self._run_once_accepted_ts = ts
            self._wake_event.set()
            return True

    # ---- 状态查询（业务循环调用） ----------------------------------

    @property
    def stop_after_file(self) -> bool:
        return self._stop_after_file

    @property
    def stop_after_round(self) -> bool:
        return self._stop_after_round

    def is_aborted(self) -> bool:
        """是否收到 fast 停止指令。"""
        return self._abort_event.is_set()

    def stop_requested(self) -> bool:
        """是否收到任意停止指令（调度主循环的退出条件）。"""
        return self._stop_after_file or self._stop_after_round or self._abort_event.is_set()

    def should_stop_before_file(self) -> bool:
        """文件级边界检查：当前文件传完后是否应停止。

        graceful 与 fast 在此生效（fast 通常已在 chunk 边界中止传输）；
        slow 不在此停止，本轮任务继续执行。
        """
        return self._stop_after_file or self._abort_event.is_set()

    # 任务循环与文件循环的停止语义一致，共用同一判定
    should_stop_before_task = should_stop_before_file

    def wait_for_wake(self, timeout: float) -> bool:
        """阻塞等待任意唤醒信号或超时（调度睡眠用）。

        返回 True 表示期间有唤醒信号到达，唤醒事件随返回消费（清空），
        是否提前结束睡眠由调用方结合停止标志判断；返回 False 表示
        超时自然醒。
        """
        woken = self._wake_event.wait(timeout)
        if woken:
            self._wake_event.clear()
        return woken

    # ---- 外部同步进程登记（sync 模式） ------------------------------

    def register_process(self, process: subprocess.Popen[Any]) -> None:
        with self._lock:
            self._children.append(process)

    def unregister_process(self, process: subprocess.Popen[Any]) -> None:
        with self._lock:
            if process in self._children:
                self._children.remove(process)

    def terminate_children(self, logger: Logger | None = None) -> None:
        """终止所有已登记的外部进程及其后代。"""
        with self._lock:
            children = list(self._children)
        for process in children:
            terminate_process_tree(process.pid, logger=logger)


def install_abort_check(coordinator: LifecycleCoordinator) -> None:
    """把 fast 中止信号接入 fs.transfer 的 chunk 边界。"""
    fs.set_abort_check(coordinator.is_aborted)


def install_sigterm_fast_stop(action: Callable[[], None], logger: Logger | None = None) -> None:
    """把 SIGTERM（容器 / 系统停服信号）映射为 fast 停止动作。

    ``docker stop`` 与 systemd 等均以 SIGTERM 请求停止，由此接入
    ``stop --fast`` 语义（Windows 上处理器可安装但系统不投递
    SIGTERM，安装无副作用；SIGKILL 不可捕获，维持现状）。处理器在
    主线程的任意字节码边界运行，内部只派生线程、不直接执行动作：
    sync 循环持续打日志，处理器内打日志可能重入 logging 锁死锁，
    ``request_abort`` 又含 psutil 终止（最长数秒阻塞）。线程执行与
    ``ControlWatcher`` 的回调线程同构。

    Args:
        action: fast 停止动作（无参可调用），在新守护线程中执行。
        logger: 安装失败时的告警出口。非主线程调用 ``signal.signal``
            会抛 ValueError，此时不中止启动，仅降级为无 SIGTERM 处理。
    """

    def _handler(_signum: int, _frame: Any) -> None:
        threading.Thread(target=action, daemon=True, name="sigterm-stop").start()

    try:
        signal.signal(signal.SIGTERM, _handler)
    except (ValueError, OSError) as e:
        if logger is not None:
            logger.warning(f"SIGTERM 停止处理器安装失败（{e}），容器停止将依赖宽限期后的强杀。")
