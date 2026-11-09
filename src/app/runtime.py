"""运行路径：初始化装配、任务执行与调度循环。

前台运行与 start 拉起的分离式实例最终都进入 run_client()：
无 [schedule] 时一次性运行，否则按 cron/interval 常驻。
"""

import datetime
import os
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any

from croniter import croniter
from pydantic import ValidationError

from src import fs
from src.app import instance, paths
from src.app.config import BootstrapConfig, ConfigError, ConfigManager, ScheduleConfig
from src.app.context import AppContext
from src.app.instance import SingleInstance
from src.app.lifecycle import install_abort_check, install_sigterm_fast_stop
from src.core import process_task
from src.core.state import StateManager
from src.core.types import TaskOutcome
from src.output.logger import Logger, setup_logger
from src.output.presentation import fmt_timespan
from src.validation import format_validation_error


def _reload_config_if_changed(manager: ConfigManager) -> None:
    cfg = manager.reload()
    if cfg is None:
        return

    ctx = AppContext.get()
    if cfg.used_backup:
        ctx.logger.warning("新配置加载失败，已回退到备份配置。")

    ctx.update_config(cfg.config, cfg.tasks)


def run_tasks() -> None:
    ctx = AppContext.get()
    coordinator = ctx.lifecycle
    ctx.logger.info("=" * 80, raw=True)
    ctx.logger.info("脚本开始执行……")
    start_time = time.time()

    specs = ctx.tasks

    if not specs:
        ctx.logger.error("配置文件没有任务可以执行。")
        sys.exit(1)

    # 依赖满足判定是运行期语义：finished 每轮重置——依赖语义为"同一轮内"，
    # 与 cron/interval 每轮全量重跑的调度语义一致。任务的配置校验与
    # 依赖静态校验已在加载期完成，此处只做编排。
    finished: set[str] = set()

    for idx, spec in enumerate(specs):
        if coordinator.should_stop_before_task():
            break

        if spec.blocked:
            # 具体错误日志已在加载期给出，此处静默跳过
            continue

        unmet = [dep for dep in spec.depends_on if dep not in finished]
        if unmet:
            ctx.logger.error(f"前置任务 {'、'.join(unmet)} 未完成，跳过该任务。")
            continue

        ctx.logger.info(f"-----开始 {len(specs)} 个任务中的第 {idx + 1} 个-----")

        outcome = process_task(spec, now=start_time)

        ctx.logger.info("当前任务结束。")

        if spec.task_id and outcome is TaskOutcome.COMPLETED:
            finished.add(spec.task_id)

    end_time = time.time()
    exec_time = end_time - start_time
    if not coordinator.stop_requested():
        ctx.logger.info(f"所有任务执行完毕，总耗时: {fmt_timespan(exec_time)}。")

    ctx.logger.info(f"{'=' * 80}", raw=True)


def _enter_phase(ctx: AppContext, phase: str, next_run: float | None = None) -> None:
    """切换实例调度相位：协调器内存（权威源）+ 状态文件（跨进程投影）。

    run-once 指令的受理判定读内存相位；状态文件供 run-once 子命令
    跨进程预检与受理回执确认。
    """
    ctx.lifecycle.set_phase(phase)
    instance.write_state(ctx.instance_name, phase, next_run, ctx.lifecycle.run_once_accepted_ts)


def _execute_tasks(manager: ConfigManager) -> None:
    """执行一轮任务，支持配置热重载。"""
    _enter_phase(AppContext.get(), instance.PHASE_RUNNING)
    try:
        _reload_config_if_changed(manager)
        run_tasks()
    except fs.TransferAbortedError:
        # stop --fast：任务被丢弃，不做完成/失败定性；
        # 停止确认与退出日志由指令分发点与 run_client 的 finally 负责
        pass
    except Exception as e:  # noqa: BLE001
        ctx = AppContext.get()
        ctx.logger.error(f"发生未知错误: {e}")


def _load_and_init(config_path: str, console: bool | None = None) -> ConfigManager:
    """加载配置文件并执行初始化流程。

    Args:
        console: 是否输出到控制台；None 时按环境变量判断（脱离终端
            运行的实例没有可用控制台，仅写日志文件）。
    Returns:
        ConfigManager 实例。
    """
    if not os.path.exists(config_path):
        print(f"Error: 配置文件 {config_path} 未找到。")
        sys.exit(1)

    raw_config = ConfigManager.load_toml(config_path)

    instance_name, invalid = instance.resolve_identity(raw_config)
    if console is None:
        console = os.environ.get(instance.DETACHED_ENV) != "1"

    # bootstrap 段校验先于日志系统就位，失败走 print + exit 同通道
    try:
        bootstrap = BootstrapConfig.model_validate(raw_config)
    except ValidationError as e:
        print(f"Error: 配置文件无效：{format_validation_error(e)}")
        sys.exit(1)

    log_dir = paths.resolve_log_dir(bootstrap.log_dir)
    logger = setup_logger(
        log_dir,
        bootstrap.max_log_files,
        bootstrap.log_level,
        prefix=instance_name,
        console=console,
        # 桥接 fs 包与实例控制模块的 logger（组合根注入，output 层不感知业务模块）
        bridge_loggers=("src.fs", instance.__name__),
    )

    # 状态库与配置管理器随实例生命周期创建/关闭
    state = StateManager(os.path.join(paths.resolve_data_dir(bootstrap.data_dir), "state.db"))

    manager = ConfigManager(config_path, logger)
    try:
        cfg = manager.load(raw_config)
    except ConfigError as e:
        # 首次加载的致命配置错误：干净退出（热重载路径由 reload 的备份回退兜底）
        state.close()
        logger.critical(f"配置文件无效：{e}")
        sys.exit(1)

    AppContext.init(logger, cfg.config, cfg.tasks, state=state, instance_name=instance_name)

    if invalid:
        logger.warning("instance_name 配置值非法，已使用默认实例标识。")
    return manager


def _interruptible_sleep(seconds: float) -> bool:
    """可被停止指令打断的睡眠。返回 True 表示收到停止指令（调度应退出）。

    受理的 run-once 同样打断睡眠：返回 False，调用方随即进入下一轮
    执行（下次运行时间由调度主循环重新计算）。唤醒事件由
    ``wait_for_wake`` 随返回消费，不会残留影响后续睡眠时长。
    """
    coordinator = AppContext.get().lifecycle
    remaining = seconds
    while remaining > 0:
        if coordinator.stop_requested():
            return True
        chunk = min(remaining, 1.0)
        if coordinator.wait_for_wake(chunk):
            # 停止指令优先于 run-once 唤醒：前者退出调度，后者立即执行
            return coordinator.stop_requested()
        remaining -= chunk
    return coordinator.stop_requested()


def _run_schedule(manager: ConfigManager) -> None:
    """按 [schedule] 配置驱动任务；未配置则一次性运行。

    [schedule] 的合法性已在加载期校验（GlobalConfig.schedule），此处仅分派。
    """
    ctx = AppContext.get()
    schedule = ctx.config.schedule

    if schedule is None or schedule.mode is None:
        run_tasks()
    elif schedule.mode == "cron":
        _run_cron(manager, schedule)
    else:
        _run_interval(manager, schedule)


def _run_first_round(manager: ConfigManager, schedule: ScheduleConfig) -> bool:
    """按 first_run_delay 执行首轮任务；返回 False 表示调度应退出。

    -1（默认）不在此执行——等待至下个调度时间点的语义由调度主循环
    实现；0 表示启动后立即执行；正数表示等待指定秒数后执行首轮，
    之后的轮次仍按调度周期进行。
    """
    ctx = AppContext.get()
    first_run_delay = schedule.first_run_delay
    if first_run_delay < 0:
        return True

    ctx.logger.info(f"等待 {first_run_delay} 秒后开始执行任务。")
    # 首轮延迟等待期视为调度休眠期：run-once 可提前唤醒执行首轮
    _enter_phase(ctx, instance.PHASE_SLEEPING, time.time() + first_run_delay)
    if _interruptible_sleep(first_run_delay):
        return False

    _execute_tasks(manager)
    return True


def _run_cron(manager: ConfigManager, schedule: ScheduleConfig) -> None:
    ctx = AppContext.get()
    cron_expr = schedule.cron_expr
    if not cron_expr:
        # 模型校验已拦截缺失/空串；此守卫仅作类型收窄的防御
        print("cron 模式需要配置 cron_expr。")
        sys.exit(1)

    ctx.logger.info(f"已设置定时任务 (cron 模式): {cron_expr}。")

    if not _run_first_round(manager, schedule):
        return

    while True:
        # 停止指令可能在上一轮任务执行期间到达：直接退出，不再预告下一轮
        if ctx.lifecycle.stop_requested():
            return
        now = datetime.datetime.now()
        cron = croniter(cron_expr, now)
        next_run = cron.get_next(datetime.datetime)
        sleep_seconds = (next_run - now).total_seconds()

        ctx.logger.info(f"下一次执行时间: {next_run.strftime('%Y-%m-%d %H:%M:%S')}。\n")
        _enter_phase(ctx, instance.PHASE_SLEEPING, next_run.timestamp())
        if _interruptible_sleep(sleep_seconds):
            return

        _execute_tasks(manager)


def _run_interval(manager: ConfigManager, schedule: ScheduleConfig) -> None:
    interval_seconds = schedule.interval_seconds
    if interval_seconds is None or interval_seconds <= 0:
        # 模型校验已拦截；此守卫仅作类型收窄的防御（同 _run_cron）
        print("interval 模式需要配置有效的 interval_seconds (大于0的数字)。")
        sys.exit(1)

    ctx = AppContext.get()
    ctx.logger.info(f"已设置定时任务 (interval 模式): 每 {interval_seconds} 秒执行一次。")

    if not _run_first_round(manager, schedule):
        return

    while True:
        # 停止指令可能在上一轮任务执行期间到达：直接退出，不再预告下一轮
        if ctx.lifecycle.stop_requested():
            return
        next_run = datetime.datetime.now() + datetime.timedelta(seconds=interval_seconds)
        ctx.logger.info(f"下一次执行时间: {next_run.strftime('%Y-%m-%d %H:%M:%S')}。\n")
        _enter_phase(ctx, instance.PHASE_SLEEPING, next_run.timestamp())
        if _interruptible_sleep(interval_seconds):
            return

        _execute_tasks(manager)


def _log_stop_received(logger: Logger, mode: str) -> None:
    """停止指令接收的唯一确认日志（停止序列的第一条）。

    三种模式共用一套句式，不区分日志级别；graceful 即 stop 默认的
    中速停止。
    """
    if mode == instance.STOP_FAST:
        logger.info("收到快速停止指令，正在退出……")
    elif mode == instance.STOP_SLOW:
        logger.info("收到慢速停止指令，正在退出……")
    else:
        logger.info("收到中速停止指令，正在退出……")


def _make_stop_action(ctx: AppContext) -> Callable[[str], None]:
    """把停止模式映射为协调器请求；控制文件指令与 SIGTERM 共用。"""
    lifecycle = ctx.lifecycle
    logger = ctx.logger

    def _apply(mode: str) -> None:
        _log_stop_received(logger, mode)
        if mode == instance.STOP_FAST:
            lifecycle.request_abort(logger)
        elif mode == instance.STOP_SLOW:
            lifecycle.request_stop_after_round()
        else:
            lifecycle.request_stop_after_file()

    return _apply


def _make_command_dispatcher(
    ctx: AppContext, stop_action: Callable[[str], None]
) -> Callable[[str, dict[str, Any]], None]:
    """把控制文件指令（stop / run-once）映射为协调器请求。"""

    def _dispatch(cmd: str, payload: dict[str, Any]) -> None:
        if cmd == instance.CMD_RUN_ONCE:
            # 仅休眠期受理；未受理静默忽略，反馈由 run-once 终端负责。
            # 指令 ts 经协调器记账为受理回执，随状态文件持久化
            if ctx.lifecycle.request_run_now(float(payload["ts"])):
                ctx.logger.info("收到立即执行指令，正在唤醒调度循环……")
            return
        stop_action(payload["mode"])

    return _dispatch


def _log_ffmpeg_source(logger: Logger) -> None:
    """启动时记录 FFmpeg 的解析结果与版本（内置优先），支撑问题诊断。

    冻结形态下内置目录已在入口前置到 PATH（src/app/paths.py）；此处读取
    版本横幅仅作诊断输出，任何失败不阻塞运行（后续调用失败另有报错）。
    """
    ffmpeg_dir = paths.bundled_ffmpeg_dir()
    source = f"内置（{ffmpeg_dir}）" if ffmpeg_dir is not None else "系统 PATH"
    version = ""
    try:
        result = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=10,
            check=False,
        )
        if result.returncode == 0 and result.stdout:
            version = result.stdout.splitlines()[0].strip()
    except OSError, ValueError, subprocess.SubprocessError:
        pass
    logger.info(f"FFmpeg 来源: {source}{'：' + version if version else ''}。")


def run_client() -> None:
    config_path = paths.config_path()
    manager = _load_and_init(config_path)

    ctx = AppContext.get()
    _log_ffmpeg_source(ctx.logger)
    install_abort_check(ctx.lifecycle)
    stop_action = _make_stop_action(ctx)
    dispatch = _make_command_dispatcher(ctx, stop_action)
    # 容器/系统停服的 SIGTERM 按 stop --fast 处理：处理器只派生线程，
    # 真正的停止动作与控制文件指令走同一条分发路径
    install_sigterm_fast_stop(lambda: stop_action(instance.STOP_FAST), ctx.logger)

    try:
        # 实例锁覆盖整个进程生命周期：运行中与调度睡眠期都属于"实例存活"，
        # stop 子命令靠锁探测判断目标是否存在
        with SingleInstance(ctx.instance_name, ctx.logger):
            # 相位先于监视线程就位，指令受理判定不会读到未初始化状态
            _enter_phase(ctx, instance.PHASE_STARTING)
            instance.ControlWatcher(ctx.instance_name, dispatch).start()
            _run_schedule(manager)
    except fs.TransferAbortedError:
        # stop --fast：一次性运行被中止即退出，无需额外处理
        pass
    finally:
        ctx.require_state().close()
        ctx.logger.info("MediaConveyor 已退出。")
