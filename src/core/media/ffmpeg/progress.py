"""FFmpeg progress 协议：块解析与中文进度行渲染。

渲染语义（三种输出形态共用的唯一格式，见 format_progress）：
「持续时间」以「已运行 / ETA」两段显示，ETA 以平均速率法估算剩余
墙钟时间（见 _format_eta）；输出时间戳以「out_time / 总时长」两段
显示；媒体总时长由 execute 模块从 stderr 启动横幅嗅探
（parse_banner_duration，首个可解析者生效），未知（横幅缺失、模板
含 -hide_banner、管道输入等）时退化为占位符；横幅为完整输入时长，
模板含 -ss / -t 裁剪时分母与估算偏大（见 _format_eta）。
"""

from dataclasses import dataclass

from src.output.presentation import fmt_size

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
    """计时器格式：进程启动至今的 wall time，恒为 MM:SS（分钟位不封顶）。"""
    total = max(0, int(seconds))
    minutes, secs = divmod(total, 60)
    return f"{minutes:02d}:{secs:02d}"


def _format_out_time(out_time: str) -> str:
    """输出时间戳：解析后重排为 MM:SS（负起始偏移保留符号）；缺失为占位符。

    空串与 N/A 不匹配 ``[-]HH:MM:SS`` 形状，由解析失败统一落占位符。
    """
    seconds = _parse_hhmmss(out_time)
    if seconds is None:
        return _PROGRESS_PLACEHOLDER
    sign = "-" if seconds < 0 else ""
    total = int(abs(seconds))
    minutes, secs = divmod(total, 60)
    return f"{sign}{minutes:02d}:{secs:02d}"


def _format_duration(seconds: float | None) -> str:
    """媒体总时长：秒 → MM:SS（分钟位不封顶）；未知或非正数为占位符。"""
    if seconds is None or seconds <= 0:
        return _PROGRESS_PLACEHOLDER
    return _format_timer(seconds)


def _parse_hhmmss(value: str) -> float | None:
    """``[-]HH:MM:SS[.小数]`` 时间戳 → 秒；N/A、空串或形状不符返回 None。

    progress 协议的 out_time（微秒小数）与启动横幅的 Duration（百分秒
    小数）共用此解析；小时位不限两位宽度，负号保留为负值（负起始偏移
    的 out_time，如 -00:00:00.020000）。
    """
    text = value.strip()
    sign = -1.0 if text.startswith("-") else 1.0
    parts = text.removeprefix("-").split(":")
    if len(parts) != 3 or not all(parts):
        return None
    try:
        seconds_total = float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
    except ValueError:
        return None
    return sign * seconds_total


# ffmpeg 启动横幅的时长行前缀（stderr 行经读取线程 strip，无前导空白）：
# Duration: 00:05:23.42, start: 0.000000, bitrate: 8543 kb/s
_DURATION_PREFIX = "Duration: "


def parse_banner_duration(line: str) -> float | None:
    """从 ffmpeg 启动横幅行解析输入媒体时长（秒）；非时长行或 N/A 返回 None。

    多输入命令存在多条时长行时取首个可解析值（通常即主媒体输入）；
    由 execute 的 stderr 消费循环逐行调用。
    """
    if not line.startswith(_DURATION_PREFIX):
        return None
    value = line[len(_DURATION_PREFIX) :].split(",", 1)[0]
    seconds = _parse_hhmmss(value)
    return seconds if seconds is not None and seconds > 0 else None


def _format_eta(progress: FFmpegProgress, elapsed: float, total_duration: float | None) -> str:
    """按开局以来的平均处理速率推算剩余墙钟时间（恒 MM:SS，分钟位不封顶）。

    平均速率 = 已处理媒体时长（out_time）÷ 已运行墙钟时间，恒以累计值
    计算：不依赖 ffmpeg speed 字段的版本语义（瞬时 / 累计不一），30 秒
    渲染节流下数值平滑。end 快照恒为 00:00；已处理时长尚未产出
    （out_time 缺失 / 为 0 / 为负）或总时长未知时以占位符表示。
    """
    if progress.progress == "end":
        return _format_timer(0)
    out_seconds = _parse_hhmmss(progress.out_time)
    if out_seconds is None or out_seconds <= 0:
        return _PROGRESS_PLACEHOLDER
    if total_duration is None or total_duration <= 0:
        return _PROGRESS_PLACEHOLDER
    remaining = max(0.0, total_duration - out_seconds)
    return _format_timer(elapsed * remaining / out_seconds)


def format_progress(progress: FFmpegProgress, elapsed: float, total_duration: float | None) -> str:
    """渲染中文进度行（三种输出形态共用的唯一格式，见模块 docstring）。

    total_duration 为 execute 从 stderr 启动横幅解析出的媒体总时长
    （秒）：作为输出时间戳的分母显示（未知为占位符），并供
    「持续时间」字段的 ETA 估算，见 _format_eta。
    """
    size = (
        fmt_size(progress.total_size, binary=True)
        if progress.total_size is not None
        else _PROGRESS_PLACEHOLDER
    )
    state = _PROGRESS_STATE.get(progress.progress, progress.progress)
    fields = [
        state,
        f"持续时间：{_format_timer(elapsed)} / {_format_eta(progress, elapsed, total_duration)}",
        f"输出时间戳：{_format_out_time(progress.out_time)} / {_format_duration(total_duration)}",
        f"当前帧：{progress.frame or _PROGRESS_PLACEHOLDER}",
        f"码率：{progress.bitrate or _PROGRESS_PLACEHOLDER}",
        f"体积：{size}",
        f"帧率：{progress.fps or _PROGRESS_PLACEHOLDER}",
        f"速率：{progress.speed or _PROGRESS_PLACEHOLDER}",
        f"复制帧：{progress.dup_frames or _PROGRESS_PLACEHOLDER}",
        f"丢弃帧：{progress.drop_frames or _PROGRESS_PLACEHOLDER}",
    ]
    return " | ".join(fields)
