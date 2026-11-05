"""FFmpeg 命令执行：命令组装、子进程托管、progress 协议消费与超时控制。

命令组装分两步：output_filename 模板（仅 {basename}）先渲染出输出
文件名，与 dest 子目录结构拼成 {output} 的值；参数模板再以
{input}/{output} 填充。完整命令由三部分拼成：ffmpeg 可执行文件
（经 PATH 解析，冻结形态解析到内置版本）→ 程序预置参数
（FFMPEG_PRESET_ARGS）→ 用户参数模板。占位符的值由本模块按运行
平台自动加引号，模板中无需手写引号；支持项之外的花括号占位符在
填充期显式报错。

子进程登记到生命周期协调器（register_process）：stop --fast 到达时
整树终止，不留孤儿 ffmpeg；停止指令在输出流的轮询间隙被消费，中止
以 TransferAbortedError 向上传播，与 fs 层的中止语义一致。

进度走 FFmpeg progress 协议：程序预置 -nostats 关闭 stderr 的周期
stats 行、-progress pipe:1 让进度块写入 stdout。stdout 读取线程以
progress= 行为块边界切块，主轮询循环把块解析为快照并渲染成中文进度行
（format_progress）经 logger 输出——raw 模式落笔（不带时间戳头部），
以 30 秒节流，无新块时也以最新快照渲染，计时器持续走动。progress=end
的快照在进程结束后追加一次终写状态行（绕过节流），异常结束（失败 /
超时 / fast 中止，不会有 end 块）不追加，最后一行进度原样保留。取消
「FFmpeg 运行报告」行——终态由终写进度行承载。
"""

import queue
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.fs import TransferAbortedError
from src.output.logger import Logger
from src.output.presentation import fmt_size
from src.platform_info import is_windows

if TYPE_CHECKING:
    from src.app.lifecycle import LifecycleCoordinator

# 进度行日志的节流间隔（秒）
_PROGRESS_LOG_INTERVAL = 30.0

# 输出流轮询间隙：停止/超时检查与行消费的粒度
_POLL_INTERVAL = 0.5

# 失败时从错误输出尾部截取的行数（ffmpeg 的报错在输出末尾）
_ERROR_TAIL_LINES = 20

# 程序注入的 ffmpeg 可执行文件：经 shell 的 PATH 解析；冻结形态由
# paths.ensure_bundled_ffmpeg_on_path 把内置目录前置到 PATH，命令解析到
# 内置版本。二进制不进用户模板：模板只填 FFmpeg 参数。
FFMPEG_BIN = "ffmpeg"

# 程序预置参数：拼在用户参数模板之前，后续新增程序侧命令行注入项只改此处。
# - -y：输出文件已存在时直接覆盖（part 文件机制下正常不会冲突，防极端
#   情形下 ffmpeg 交互式询问挂起进程）；
# - -nostdin：禁用 stdin（ffmpeg 默认读 stdin，前台运行时按键 q 会误停
#   正在进行的转码）；
# - -nostats：关闭 stderr 的周期 stats 行（frame=/time=/speed=），错误
#   输出只剩真正的日志与报错，进度数据改由 progress 协议提供；
# - -progress pipe:1：progress 协议块写入 stdout，经读取线程切块解析
#   （见模块 docstring）。用户模板若自身写 stdout 会与进度块交叠，
#   本项目的文件型输出任务不涉及该形态。
FFMPEG_PRESET_ARGS: tuple[str, ...] = ("-y", "-nostdin", "-nostats", "-progress", "pipe:1")


class TemplateFormatError(ValueError):
    """模板填充失败：命令模板含有 {input}/{output} 之外、output_filename
    模板含有 {basename} 之外的占位符。"""


def _shell_quote(value: str) -> str:
    """按运行平台的 shell 语法为路径加引号（{input}/{output} 值的统一出口）。

    - Windows：命令经 cmd.exe（shell=True）执行，其元字符（& | < > ^ 等）
      在双引号内均按字面处理，故无条件双引号包裹；尾部反斜杠翻倍，防止
      转义收尾引号。%VAR% 展开在引号内仍会发生，路径含成对 % 的极端
      情形不受保护；
    - POSIX（Linux / macOS / Docker）：shlex.quote，覆盖全部不安全字符，
      安全字符不加引号。
    """
    if is_windows():
        if value.endswith("\\"):
            value += "\\"
        return f'"{value}"'
    return shlex.quote(value)


def build_command(template: str, filepath: str, output_path: str) -> str:
    """填充 FFmpeg 参数模板并与程序注入项组装为单行完整命令。

    - 组装顺序：FFMPEG_BIN → FFMPEG_PRESET_ARGS → 用户参数模板，
      {output} 的值因此保持在命令行末尾；
    - ``{input}`` → 源文件完整路径；``{output}`` → 输出文件完整路径
      （文件名由任务的 output_filename 模板渲染，子目录结构随源文件）；
    - 两个占位符的值由程序按运行平台自动加引号，模板中无需（也不应）
      再手写引号；
    - 多行模板合并为单行（换行替换为空格），支持 TOML 多行字符串书写；
    - 模板中的其他花括号占位符会触发 KeyError，属配置错误，显式抛出
      TemplateFormatError（str.format 的既有语义）。
    """
    try:
        raw = template.format(input=_shell_quote(filepath), output=_shell_quote(output_path))
    except (KeyError, IndexError) as e:
        raise TemplateFormatError(
            f"命令模板含有不受支持的占位符: {e}（仅支持 {{input}} 与 {{output}}）"
        ) from e
    args = raw.replace("\n", " ").replace("\r", " ").strip()
    return " ".join((FFMPEG_BIN, *FFMPEG_PRESET_ARGS, args))


def render_output_filename(template: str, basename: str) -> str:
    """渲染 output_filename 文件名模板（目前仅支持 ``{basename}``）。

    渲染失败属配置错误，抛 TemplateFormatError——与命令模板错误一样
    可在加载期判定：渲染结果只取决于模板形状，与真实文件名无关。
    """
    try:
        return template.format(basename=basename)
    except (KeyError, IndexError) as e:
        raise TemplateFormatError(
            f"output_filename 模板含有不受支持的占位符: {e}（仅支持 {{basename}}）"
        ) from e


# FFmpeg 输出瞬态文件名前缀：处理期间 {output} 实际指向
# ``.mc-ffmpeg-part.{output}``。标记不置于尾部（``.{output}.mc-ffmpeg-part``
# 形式）的原因：ffmpeg 依据输出文件的扩展名推断封装格式，未知后缀会
# 触发 "Unable to choose an output format"。与 fs/atomic.py 的 .mc-part
# 同构：目标同目录的确定性命名，处理完成后由处理器原子改名复原。
FFMPEG_PART_PREFIX = ".mc-ffmpeg-part."


def part_output_filename(out_filename: str) -> str:
    """渲染输出文件的瞬态 part 文件名（``.mc-ffmpeg-part.{output}``）。

    output_filename 在加载期已校验为纯文件名，part 文件与最终输出
    同目录同文件系统，完成后的改名（os.replace）是原子操作：观察
    目标目录的消费者在处理完成前不会看到最终文件名。完整原名保留在
    标记之后，扩展名仍位于末尾，ffmpeg 的封装格式推断不受影响。
    """
    return f"{FFMPEG_PART_PREFIX}{out_filename}"


# ---- progress 协议：解析与渲染 ----------------------------------------

# progress 协议的状态值 → 展示用词
_PROGRESS_STATE = {"continue": "进行中", "end": "已完成"}

# 进度字段缺失（N/A / 尚未产出）时的占位符
_PROGRESS_PLACEHOLDER = "--"


@dataclass(frozen=True, slots=True)
class FFmpegProgress:
    """progress 协议单块的解析结果。

    数值类字段保留 ffmpeg 的原始字符串（码率/帧率/速率的量纲与格式由
    ffmpeg 决定，展示层不做二次加工）；仅 total_size（字节）解析为 int
    供体积格式化，N/A 或缺失为 None。缺失字段以空字符串表示，由渲染层
    补占位符。
    """

    progress: str  # continue / end
    frame: str
    fps: str
    bitrate: str
    total_size: int | None
    out_time: str
    dup_frames: str
    drop_frames: str
    speed: str


def parse_progress_block(lines: list[str]) -> FFmpegProgress | None:
    """解析 progress 协议的单个 key=value 块；不含 progress 键时返回 None。

    ffmpeg 的每个进度块恒以 ``progress=`` 行收尾，无该键的内容不构成块。
    """
    fields: dict[str, str] = {}
    for line in lines:
        key, sep, value = line.partition("=")
        if sep:
            fields[key.strip()] = value.strip()
    if "progress" not in fields:
        return None
    raw_size = fields.get("total_size", "")
    return FFmpegProgress(
        progress=fields["progress"],
        frame=fields.get("frame", ""),
        fps=fields.get("fps", ""),
        bitrate=fields.get("bitrate", ""),
        total_size=int(raw_size) if raw_size.isdigit() else None,
        out_time=fields.get("out_time", ""),
        dup_frames=fields.get("dup_frames", ""),
        drop_frames=fields.get("drop_frames", ""),
        speed=fields.get("speed", ""),
    )


def _format_timer(seconds: float) -> str:
    """计时器格式：进程启动至今的 wall time，恒为 HH:MM:SS。"""
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _format_out_time(out_time: str) -> str:
    """输出时间戳：丢弃小数部分（00:00:04.200000 → 00:00:04）。"""
    if not out_time or out_time == "N/A":
        return _PROGRESS_PLACEHOLDER
    cut = out_time.find(".")
    return out_time if cut < 0 else out_time[:cut]


def format_progress(progress: FFmpegProgress, elapsed: float) -> str:
    """渲染中文进度行（三种输出形态共用的唯一格式，见模块 docstring）。"""
    size = (
        fmt_size(progress.total_size, binary=True)
        if progress.total_size is not None
        else _PROGRESS_PLACEHOLDER
    )
    fields = [
        f"持续时间：{_format_timer(elapsed)}",
        f"输出时间戳：{_format_out_time(progress.out_time)}",
        f"当前帧：{progress.frame or _PROGRESS_PLACEHOLDER}",
        f"码率：{progress.bitrate or _PROGRESS_PLACEHOLDER}",
        f"体积：{size}",
        f"帧率：{progress.fps or _PROGRESS_PLACEHOLDER}",
        f"速率：{progress.speed or _PROGRESS_PLACEHOLDER}",
        f"复制帧：{progress.dup_frames or _PROGRESS_PLACEHOLDER}",
        f"丢弃帧：{progress.drop_frames or _PROGRESS_PLACEHOLDER}",
    ]
    state = _PROGRESS_STATE.get(progress.progress, progress.progress)
    return f"【{state}】 " + " | ".join(fields)


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
            finished_line = format_progress(parsed, time.time() - start_time)


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
    - 进度：progress 协议块解析渲染后经 logger 输出（raw，不带时间戳
      头部，30 秒节流）；progress=end 的快照在进程结束后追加一次终写
      状态行，异常结束（失败/超时/中止）不追加，最后一行进度原样保留。
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

    try:
        while True:
            if coordinator.is_aborted():
                coordinator.terminate_children(logger)
                raise TransferAbortedError()

            last_progress, finished_line = _absorb_blocks(
                blocks, last_progress, finished_line, start_time
            )
            # 无新块也以最新快照渲染：计时器持续走动；落笔以 30 秒节流，
            # end 之后不再产生新的进度行
            now = time.time()
            if (
                last_progress is not None
                and finished_line is None
                and now - last_progress_log >= _PROGRESS_LOG_INTERVAL
            ):
                logger.info(format_progress(last_progress, now - start_time), raw=True)
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
                    logger.error(f"FFmpeg 处理超时（{timeout:g} 秒），正在终止进程: {rel_path}")
                    coordinator.terminate_children(logger)
                    process.wait()
                    break
                continue

            error_output.append(line)
    finally:
        coordinator.unregister_process(process)
        # end 块可能在进程退出与最后一次排空间才抵达：先收尾 stdout 读取
        # 线程并清空剩余块，再以 end 快照追加一次终写状态行（绕过节流）；
        # 异常结束（失败/超时/中止）无 end 块，不追加，最后一行进度原样保留
        progress_reader.join(timeout=_POLL_INTERVAL)
        last_progress, finished_line = _absorb_blocks(
            blocks, last_progress, finished_line, start_time
        )
        if finished_line is not None:
            logger.info(finished_line, raw=True)

    process.wait()
    elapsed = time.time() - start_time

    if timed_out:
        _log_error_tail(error_output, logger, rel_path)
        return False

    if process.returncode == 0:
        logger.info(f"FFmpeg 处理完成: {rel_path}，耗时 {elapsed:.1f} 秒。")
        return True

    _log_error_tail(error_output, logger, rel_path)
    return False


def _log_error_tail(error_output: list[str], logger: Logger, rel_path: str) -> None:
    """失败时输出错误信息的末尾若干行（ffmpeg 的报错位于输出末尾）。"""
    tail = "\n".join(error_output[-_ERROR_TAIL_LINES:])
    logger.error(f"FFmpeg 处理失败: {rel_path}，原因:\n{tail}")
