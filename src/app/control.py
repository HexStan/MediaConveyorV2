"""控制子命令 start / log / stop / run-once。

一次性 CLI：只从配置解析实例标识与日志目录（宽松模式，配置异常时
回退默认值，保证 log/stop/run-once 始终可用），不初始化运行环境。
start 以分离式子进程拉起运行路径（src.app.runtime）；run-once 向
处于调度休眠期的运行实例投递立即执行指令，随后进入日志跟随。
--quiet 时不进入日志跟随：确认信号（start 为实例锁被持有，run-once
为指令受理回执）后输出一行确认即返回，未确认按失败退出。
"""

import os
import subprocess
import sys
import time
from typing import Any

from src.app import instance, paths
from src.app.config import ConfigManager
from src.output.logtail import LogFollower
from src.platform_info import is_windows

# stop --fast 发出指令后等待实例自行退出的时限，超时则强杀
_FAST_STOP_TIMEOUT = 5.0

# run-once 发出指令后等待受理回执（last_run_once）落盘的时限，超时判定为未受理
_RUN_ONCE_ACCEPT_TIMEOUT = 2.0

# start --quiet 发出拉起后等待实例锁被持有的时限：覆盖子进程解释器/
# 冻结 exe 启动与配置加载的时间预算。锁在运行路径初始化完成后才获取
# （SingleInstance.__enter__），锁被持有即确认实例已经启动
_START_CONFIRM_TIMEOUT = 10.0

# run-once 指令未被受理（实例恰好转入执行期的竞态）的提示文案，
# 非 quiet 路径打印到终端、quiet 路径打印到 stderr 后按失败退出
_RUN_ONCE_IGNORED_HINT = (
    "提示: 实例未确认受理，指令可能已被忽略。实例可能恰好在指令送达前开始执行。"
)


def _load_control_context(strict: bool = False) -> tuple[str, str]:
    """控制子命令的配置读取，返回 (instance_name, log_dir)。

    与运行路径不同，这里不做完整校验，只解析实例标识与日志目录。
    strict=True（start）时配置缺失或解析失败直接退出；宽松模式
    （log/stop）回退默认实例标识与日志目录，保证配置异常时依然可用。
    """
    path = paths.config_path()
    raw_config: dict[str, Any] = {}
    if os.path.exists(path):
        try:
            raw_config = ConfigManager.load_toml(path)
        except Exception as e:  # noqa: BLE001
            if strict:
                print(f"Error: 配置文件解析失败: {e}")
                sys.exit(1)
            print(f"警告: 配置文件解析失败（{e}），按默认实例标识处理。")
    else:
        if strict:
            print(f"Error: 配置文件 {path} 未找到。")
            sys.exit(1)
        print(f"警告: 配置文件 {path} 未找到，按默认实例标识处理。")

    instance_name, invalid = instance.resolve_identity(raw_config)
    if invalid:
        print(f"警告: instance_name 配置值非法，按默认实例标识 {instance_name} 处理。")
    # 宽松模式：log_dir 仅做类型守卫回退，
    # 不做完整校验——log/stop 在配置异常时必须依然可用
    raw_log_dir = raw_config.get("log_dir", "./logs")
    log_dir = raw_log_dir if isinstance(raw_log_dir, str) else "./logs"
    return instance_name, paths.resolve_log_dir(log_dir)


def _command_hint() -> str:
    prog = os.path.basename(sys.argv[0]) or "main.py"
    return prog


def _spawn_detached() -> int:
    """以脱离终端的方式拉起自身，返回子进程 PID。"""
    if paths.is_frozen():
        cmd = [sys.executable]
    else:
        cmd = [sys.executable, os.path.join(paths.app_base_dir(), "main.py")]

    env = os.environ.copy()
    env[instance.DETACHED_ENV] = "1"

    kwargs: dict[str, Any] = {}
    if is_windows():
        # CREATE_NO_WINDOW：进程带一个不可见的控制台。不用 DETACHED_PROCESS
        # 是因为源码模式下 venv 的 python.exe 是跳板程序，它重新 spawn 的
        # 真实解释器不继承该标志，会被 Windows 分配一个可见的空控制台窗口；
        # 而隐藏控制台会被正常继承，跳板链路同样无窗口。
        # pyrefly 按 Linux 语义检查（见 pyproject.toml 的 [tool.pyrefly]），两个
        # creationflags 常量是 Windows 专属，此处运行时已被平台判定守卫
        kwargs["creationflags"] = (
            subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[pyrefly:missing-attribute]
        )
    else:
        kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            **kwargs,
        )
    except OSError as e:
        print(f"Error: 无法以后台形式启动: {e}")
        sys.exit(1)
    return proc.pid


def _follow_logs(lines: int = 100) -> None:
    """滚动显示运行日志，底部状态栏显示实例存活状态。

    Ctrl+C 结束跟随（不影响实例运行）。
    """
    instance_name, log_dir = _load_control_context()

    def _probe_status() -> tuple[bool, int | None]:
        return instance.is_running(instance_name), instance.read_pid(instance_name)

    follower = LogFollower(log_dir, instance_name, status_provider=_probe_status)
    try:
        follower.follow(lines)
    except KeyboardInterrupt:
        pass


def cmd_start(quiet: bool = False) -> None:
    """start 子命令：脱离终端启动实例，随后滚动显示运行日志。

    --quiet 时不进入日志跟随：轮询实例锁确认子进程已启动（锁在运行
    路径初始化完成后才获取，被持有即启动确认），输出一行确认后返回；
    超时未确认按失败退出。
    """
    instance_name, _log_dir = _load_control_context(strict=True)
    if instance.is_running(instance_name):
        pid = instance.read_pid(instance_name)
        where = f" (PID {pid})" if pid else ""
        print(f"已有同名实例正在运行{where}，无需重复启动。")
        sys.exit(1)

    pid = _spawn_detached()
    if quiet:
        try:
            started = _wait_instance_started(instance_name)
        except KeyboardInterrupt:
            # 放弃等待：实例仍在后台启动，可用 log 查看进度
            return
        if started:
            print(f"实例已启动 (PID {pid})。")
        else:
            print(
                f"Error: 等待启动确认超时（{_START_CONFIRM_TIMEOUT:g} 秒），"
                f"实例可能启动失败。可执行 {_command_hint()} log 查看运行日志。",
                file=sys.stderr,
            )
            sys.exit(1)
        return

    print(f"实例已在后台启动，本终端可随时退出。如要停止运行请执行 {_command_hint()} stop。")
    print("正在滚动显示运行日志……\n")
    _follow_logs()


def cmd_log(lines: int) -> None:
    _follow_logs(lines)


def cmd_stop(fast: bool, slow: bool) -> None:
    """stop 子命令：向运行实例发送停止指令并等待退出。

    静默设计：发送与等待过程不播报，实例日志是唯一进度来源；
    仅在无实例与出错时输出，退出确认统一为一行"实例已退出。"。
    """
    instance_name, _log_dir = _load_control_context()
    if not instance.is_running(instance_name):
        print("没有正在运行的实例。")
        return

    if fast:
        mode = instance.STOP_FAST
    elif slow:
        mode = instance.STOP_SLOW
    else:
        mode = instance.STOP_GRACEFUL
    instance.send_stop(instance_name, mode)

    timeout = _FAST_STOP_TIMEOUT if mode == instance.STOP_FAST else None
    try:
        exited = instance.wait_for_exit(instance_name, timeout=timeout)
    except KeyboardInterrupt:
        # 放弃等待：实例仍在后台执行停止流程，可用 log 查看进度
        return

    if exited:
        print("实例已退出。")
        return

    # fast 兜底：超时未退出 → 按进程树强杀
    pid = instance.read_pid(instance_name)
    if pid is None:
        print("Error: 实例未按时退出且 PID 文件缺失，无法强制结束。", file=sys.stderr)
        sys.exit(1)
    if instance.force_kill_tree(pid):
        print("实例已退出。")
    else:
        print(
            "Error: 强制结束失败：目标进程与当前程序不符，已放弃以避免误杀。",
            file=sys.stderr,
        )
        sys.exit(1)


def _run_once_blocker(instance_name: str) -> str | None:
    """run-once 前置判定，返回拒绝原因；None 表示可以执行。

    实例存活以锁探测为准（崩溃残留的状态文件不会误判），休眠期以
    状态文件中的调度相位投影为准。
    """
    if not instance.is_running(instance_name):
        return "没有正在运行的实例。"
    state = instance.read_state(instance_name)
    if state is None:
        return "无法读取实例运行状态，无法确认实例是否处于调度休眠期。"
    if state.get("phase") != instance.PHASE_SLEEPING:
        return "实例不在调度休眠期，无法立即执行。可能正在执行任务、未配置 [schedule] 或尚未就绪。"
    return None


def _wait_instance_started(
    instance_name: str, timeout: float | None = None, poll_interval: float = 0.5
) -> bool:
    """轮询实例锁，确认分离式子进程已获锁运行（``start --quiet`` 的确认信号）。

    锁被持有即实例已完成装配并即将进入运行——与 log / stop / run-once
    公用的存活权威判定（``is_running``）同源，不新增回执协议。timeout
    缺省时在函数体内读模块常量（每次调用读取，测试可注入缩短等待）。
    """
    if timeout is None:
        timeout = _START_CONFIRM_TIMEOUT
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if instance.is_running(instance_name):
            return True
        time.sleep(poll_interval)
    return False


def _wait_run_once_accepted(
    instance_name: str,
    sent_ts: float,
    timeout: float | None = None,
    poll_interval: float = 0.5,
) -> bool:
    """轮询状态文件，等待受理回执（``last_run_once >= sent_ts``）。

    受理回执由实例记账后随每次状态写入持久化，不存在瞬时窗口——
    执行轮次再快（哪怕短于轮询间隔）也不会漏报。实例中途退出同样
    终止等待。timeout 缺省时在函数体内读模块常量（测试可注入）。
    """
    if timeout is None:
        timeout = _RUN_ONCE_ACCEPT_TIMEOUT
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not instance.is_running(instance_name):
            return False
        state = instance.read_state(instance_name)
        if state is not None:
            ack = state.get("last_run_once")
            if isinstance(ack, (int, float)) and ack >= sent_ts:
                return True
        time.sleep(poll_interval)
    return False


def cmd_run_once(quiet: bool = False) -> None:
    """run-once 子命令：让处于调度休眠期的实例立即执行一轮任务。

    预检（存活 + 休眠期）通过后投递指令并等待受理回执，随后进入
    日志跟随（同 start）。--quiet 时不进入日志跟随：受理即一行确认
    返回，未受理打印提示到 stderr 后按失败退出。实例正处于执行期的
    竞态窗口内到达的指令会被静默忽略，等待超时给出提示。
    """
    instance_name, _log_dir = _load_control_context()
    if (reason := _run_once_blocker(instance_name)) is not None:
        print(reason)
        sys.exit(1)

    sent_ts = time.time()
    instance.send_run_once(instance_name)
    accepted = _wait_run_once_accepted(instance_name, sent_ts)

    if quiet:
        # 脚本化路径：受理即一行确认返回，未受理按失败退出，不进日志跟随
        if accepted:
            print("指令已受理，实例即将执行一轮任务。")
        else:
            print(_RUN_ONCE_IGNORED_HINT, file=sys.stderr)
            sys.exit(1)
        return

    if not accepted:
        print(_RUN_ONCE_IGNORED_HINT)
    print("正在滚动显示运行日志……\n")
    _follow_logs()
