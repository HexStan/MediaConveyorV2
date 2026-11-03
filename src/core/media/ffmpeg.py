"""FFmpeg 命令执行：模板填充、子进程托管、进度流式输出与超时控制。

子进程登记到生命周期协调器（register_process）：stop --fast 到达时
整树终止，不留孤儿 ffmpeg；停止指令在输出流的轮询间隙被消费，中止
以 TransferAbortedError 向上传播，与 fs 层的中止语义一致。

进度行（含 time=/speed= 的 stderr 行）以 5 秒节流经 logger 输出——
不再区分 TTY/非 TTY 的单行覆盖打印：控制台呈现由 output 层统一负责。
"""

import queue
import subprocess
import threading
import time
from typing import TYPE_CHECKING

from src.fs import TransferAbortedError
from src.output.logger import Logger

if TYPE_CHECKING:
    from src.app.lifecycle import LifecycleCoordinator

# 进度行日志的节流间隔（秒）
_PROGRESS_LOG_INTERVAL = 5.0

# 输出流轮询间隙：停止/超时检查与行消费的粒度
_POLL_INTERVAL = 0.2

# 失败时从错误输出尾部截取的行数（ffmpeg 的报错在输出末尾）
_ERROR_TAIL_LINES = 20


class TemplateFormatError(ValueError):
    """命令模板填充失败（模板含有 {input}/{output} 之外的占位符）。"""


def build_command(template: str, filepath: str, dst_basepath: str) -> str:
    """填充命令模板并合并为单行。

    - ``{input}`` → 源文件完整路径；``{output}`` → 目标目录下保持子目录
      结构的基础文件名（不含扩展名），输出扩展名由模板自行决定；
    - 多行模板合并为单行（换行替换为空格），支持 TOML 多行字符串书写；
    - 模板中的其他花括号占位符会触发 KeyError，属配置错误，显式抛出
      TemplateFormatError（str.format 的既有语义）。
    """
    try:
        raw = template.format(input=filepath, output=dst_basepath)
    except (KeyError, IndexError) as e:
        raise TemplateFormatError(
            f"命令模板含有不受支持的占位符: {e}（仅支持 {{input}} 与 {{output}}）"
        ) from e
    return raw.replace("\n", " ").replace("\r", " ")


def _consume_stderr(process: subprocess.Popen[str], lines: queue.Queue[str]) -> None:
    """逐块读取子进程 stderr，按行拆分后投入队列（在独立线程中运行）。"""
    buffer = ""
    stream = process.stderr
    if stream is None:  # pragma: no cover - Popen(stderr=PIPE) 保证非 None
        return
    while True:
        try:
            chunk = stream.read(4096)
        except (OSError, ValueError):
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


def run_ffmpeg(
    cmd: str,
    *,
    rel_path: str,
    logger: Logger,
    coordinator: LifecycleCoordinator,
    timeout: float = 0,
) -> bool:
    """执行拼接完成的 ffmpeg 命令；返回是否成功。

    - 停止：fast 中止信号到达时终止进程树并抛 TransferAbortedError；
    - 超时：timeout > 0 时超时终止进程树，按失败处理（返回 False）；
    - 进度：含 time=/speed= 的行以 5 秒节流输出，命令结束时输出
      最终运行报告。
    """
    start_time = time.time()
    try:
        process = subprocess.Popen(
            cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
        )
    except OSError as e:
        logger.error(f"无法启动 FFmpeg: {e}")
        return False

    coordinator.register_process(process)
    lines: queue.Queue[str] = queue.Queue()
    reader = threading.Thread(target=_consume_stderr, args=(process, lines), daemon=True)
    reader.start()

    deadline = start_time + timeout if timeout > 0 else None
    timed_out = False
    error_output: list[str] = []
    final_status = ""
    last_progress_log = 0.0

    try:
        while True:
            if coordinator.is_aborted():
                coordinator.terminate_children(logger)
                raise TransferAbortedError()

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
                    logger.error(f"FFmpeg 处理超时（{timeout:g} 秒），正在终止进程: {rel_path}")
                    coordinator.terminate_children(logger)
                    process.wait()
                    break
                continue

            error_output.append(line)
            if "time=" in line or "speed=" in line:
                final_status = line
                now = time.time()
                if now - last_progress_log >= _PROGRESS_LOG_INTERVAL:
                    logger.info(f"FFmpeg 进度: {line}")
                    last_progress_log = now
    finally:
        coordinator.unregister_process(process)

    process.wait()
    elapsed = time.time() - start_time

    if timed_out:
        _log_error_tail(error_output, logger, rel_path)
        return False

    if process.returncode == 0:
        logger.info(f"FFmpeg 处理完成: {rel_path}，耗时 {elapsed:.1f} 秒。")
        if final_status:
            logger.info(f"FFmpeg 运行报告: {final_status}")
        return True

    _log_error_tail(error_output, logger, rel_path)
    return False


def _log_error_tail(error_output: list[str], logger: Logger, rel_path: str) -> None:
    """失败时输出错误信息的末尾若干行（ffmpeg 的报错位于输出末尾）。"""
    tail = "\n".join(error_output[-_ERROR_TAIL_LINES:])
    logger.error(f"FFmpeg 处理失败: {rel_path}，原因:\n{tail}")
