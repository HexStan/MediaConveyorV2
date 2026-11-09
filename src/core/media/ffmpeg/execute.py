"""FFmpeg 子进程托管：进程启动、输出流消费与停止/超时/中止终审。

子进程登记到生命周期协调器（register_process）：stop --fast 到达时
整树终止，不留孤儿 ffmpeg；停止指令在输出流的轮询间隙被消费，中止
以 TransferAbortedError 向上传播，与 fs 层的中止语义一致。

进度走 FFmpeg progress 协议：stdout 读取线程以 progress= 行为块边界
切块，主轮询循环把块解析为快照并渲染成中文进度行（渲染语义见
progress 模块）经 logger 输出——raw 模式落笔（不带时间戳头部），
以 30 秒节流，无新块时也以最新快照渲染，计时器持续走动。
progress=end 的快照在进程结束后追加一次终写状态行（绕过节流），
异常结束（失败 / 超时 / fast 中止，不会有 end 块）不追加，最后一行
进度原样保留；终态由终写进度行承载，无独立的运行报告输出。媒体总
时长优先取调用方传入的探测值（get_media_info 阶段的 ffprobe 结果），
未探测或探测无时长时自 stderr 启动横幅嗅探
（progress.parse_banner_duration）。
"""

import queue
import subprocess
import threading
import time
from typing import TYPE_CHECKING

from src.core.media.ffmpeg.progress import (
    FFmpegProgress,
    format_progress,
    parse_banner_duration,
    parse_progress_block,
)
from src.fs import TransferAbortedError
from src.output.logger import Logger
from src.output.presentation import fmt_timespan

if TYPE_CHECKING:
    from src.app.lifecycle import LifecycleCoordinator

# 进度行日志的节流间隔（秒）
_PROGRESS_LOG_INTERVAL = 30.0

# 输出流轮询间隙：停止/超时检查与行消费的粒度
_POLL_INTERVAL = 0.5

# 失败时从错误输出尾部截取的行数（ffmpeg 的报错在输出末尾）
_ERROR_TAIL_LINES = 20


def _consume_stderr(process: subprocess.Popen[str], lines: queue.Queue[str]) -> None:
    """逐块读取子进程 stderr，按行拆分后投入队列（在独立线程中运行）。"""
    buffer = ""
    stream = process.stderr
    if stream is None:  # pragma: no cover - Popen(stderr=PIPE) 保证非 None
        return
    while True:
        try:
            chunk = stream.read(4096)
        except OSError, ValueError:
            break
        if not chunk:
            break
        buffer += chunk.decode("utf-8", errors="ignore")
        while True:
            cut = min(
                (i for i in (buffer.find("\r"), buffer.find("\n")) if i != -1),
                default=-1,
            )
            if cut == -1:
                break
            line = buffer[:cut].strip()
            if line:
                lines.put(line)
            buffer = buffer[cut + 1 :]
    tail = buffer.strip()
    if tail:
        lines.put(tail)


def _consume_stdout(process: subprocess.Popen[str], blocks: queue.Queue[list[str]]) -> None:
    """逐块读取子进程 stdout 的 progress 协议输出（在独立线程中运行）。

    以 ``progress=`` 行为块边界把整块行列表投入队列；空行跳过，无
    progress= 收尾的残留行（异常终止时的半块）不构成块、直接丢弃。
    readline 逐行读取保证低延迟：行一到即返回，不等凑满读取长度。
    """
    stream = process.stdout
    if stream is None:  # pragma: no cover - Popen(stdout=PIPE) 保证非 None
        return
    block: list[str] = []
    while True:
        try:
            raw = stream.readline()
        except OSError, ValueError:
            break
        if not raw:
            break
        line = raw.decode("utf-8", errors="ignore").strip()
        if not line:
            continue
        block.append(line)
        if line.startswith("progress="):
            blocks.put(block)
            block = []


def _absorb_blocks(
    blocks: queue.Queue[list[str]],
    last_progress: FFmpegProgress | None,
    finished_line: str | None,
    start_time: float,
    total_duration: float | None,
) -> tuple[FFmpegProgress | None, str | None]:
    """消费队列中就绪的 progress 块；返回 (最新快照, end 终写行)。

    end 块即最终快照：其行以当下计时定格，之后不再产生新的进度行。
    """
    while True:
        try:
            block = blocks.get_nowait()
        except queue.Empty:
            return last_progress, finished_line
        parsed = parse_progress_block(block)
        if parsed is None:
            continue
        last_progress = parsed
        if parsed.progress == "end" and finished_line is None:
            finished_line = format_progress(parsed, time.time() - start_time, total_duration)


def run_ffmpeg(
    cmd: str,
    *,
    logger: Logger,
    coordinator: LifecycleCoordinator,
    timeout: float = 0,
    total_duration: float | None = None,
) -> bool:
    """执行拼接完成的 ffmpeg 命令；返回是否成功。

    - 停止：fast 中止信号到达时终止进程树并抛 TransferAbortedError；
      进程收尾后以中止信号终审定性——被协调器终止并回收的子进程其
      返回码不可信（waitpid 命中 ECHILD 时 subprocess 会映射为 0），
      中止在场时无论返回码一律按中止处理，不进入成功/失败分支；
    - 超时：timeout > 0 时超时终止进程树，按失败处理（返回 False）；
    - 进度：progress 协议块解析渲染后经 logger 输出（raw，不带时间戳
      头部，30 秒节流，输出时间戳的分母与「持续时间」预计总时长的
      估算均以媒体总时长为准：优先取 total_duration 传入的探测值，
      未探测或探测无时长（None）时自 stderr 启动横幅嗅探）；
      progress=end 的快照在进程结束后追加一次终写状态行，异常结束
      （失败/超时/中止）不追加，最后一行进度原样保留。
    """
    start_time = time.time()
    try:
        process = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as e:
        logger.error(f"无法启动 FFmpeg: {e}")
        return False

    coordinator.register_process(process)
    lines: queue.Queue[str] = queue.Queue()
    blocks: queue.Queue[list[str]] = queue.Queue()
    reader = threading.Thread(target=_consume_stderr, args=(process, lines), daemon=True)
    progress_reader = threading.Thread(target=_consume_stdout, args=(process, blocks), daemon=True)
    reader.start()
    progress_reader.start()

    deadline = start_time + timeout if timeout > 0 else None
    timed_out = False
    error_output: list[str] = []
    last_progress: FFmpegProgress | None = None
    finished_line: str | None = None  # progress=end 时刻的终写行
    last_progress_log = 0.0
    # 媒体总时长（秒）：初始为调用方传入的探测值，None 时自 stderr
    # 启动横幅兜底嗅探（嗅探守卫见主循环）

    try:
        while True:
            if coordinator.is_aborted():
                coordinator.terminate_children(logger)
                raise TransferAbortedError()

            last_progress, finished_line = _absorb_blocks(
                blocks, last_progress, finished_line, start_time, total_duration
            )
            # 无新块也以最新快照渲染：计时器持续走动；落笔以 30 秒节流，
            # end 之后不再产生新的进度行
            now = time.time()
            if (
                last_progress is not None
                and finished_line is None
                and now - last_progress_log >= _PROGRESS_LOG_INTERVAL
            ):
                logger.info(
                    format_progress(last_progress, now - start_time, total_duration), raw=True
                )
                last_progress_log = now

            try:
                line = lines.get(timeout=_POLL_INTERVAL)
            except queue.Empty:
                if process.poll() is not None:
                    # 进程已退出：等待 reader 线程排空管道尾部数据（有上限，
                    # 防止被残留的孙进程句柄阻塞），排空后结束循环
                    reader.join(timeout=_POLL_INTERVAL)
                    if lines.empty():
                        break
                elif deadline is not None and time.time() >= deadline:
                    timed_out = True
                    logger.error(f"FFmpeg 处理时长超出 {timeout:g} 秒的限制，正在停止处理……")
                    coordinator.terminate_children(logger)
                    process.wait()
                    break
                continue

            error_output.append(line)
            # 横幅嗅探是被动的（仅在无探测值时兜底生效）：时长行仍原样
            # 进错误缓冲，失败尾部报告不受影响
            if total_duration is None:
                total_duration = parse_banner_duration(line)
    finally:
        coordinator.unregister_process(process)
        # end 块可能在进程退出与最后一次排空间才抵达：先收尾 stdout 读取
        # 线程并清空剩余块，再以 end 快照追加一次终写状态行（绕过节流）；
        # 异常结束（失败/超时/中止）无 end 块，不追加，最后一行进度原样保留
        progress_reader.join(timeout=_POLL_INTERVAL)
        last_progress, finished_line = _absorb_blocks(
            blocks, last_progress, finished_line, start_time, total_duration
        )
        if finished_line is not None:
            logger.info(finished_line, raw=True)

    process.wait()
    elapsed = time.time() - start_time

    if coordinator.is_aborted():
        # 中止终审：被终止的子进程可能已被 psutil 回收，本进程随后
        # waitpid 命中 ECHILD，返回码会被 subprocess 映射为 0——返回码
        # 不可信。中止在场时一律抛出：不发布产物、不计失败，与循环
        # 顶部的中止路径汇合（part 清理与本轮丢弃由上层处理）。
        raise TransferAbortedError()

    if timed_out:
        _log_error_tail(error_output, logger)
        return False

    if process.returncode == 0:
        logger.info(f"FFmpeg 处理完成，耗时 {fmt_timespan(elapsed)}。")
        return True

    _log_error_tail(error_output, logger)
    return False


def _log_error_tail(error_output: list[str], logger: Logger) -> None:
    """失败时输出错误信息的末尾若干行（ffmpeg 的报错位于输出末尾）。"""
    tail = "\n".join(error_output[-_ERROR_TAIL_LINES:])
    logger.error(f"FFmpeg 处理失败:\n{tail}")
