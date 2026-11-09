"""ffprobe 封装：探测媒体文件的时长、码率、分辨率与编码信息。

任何探测异常（ffprobe 缺失、超时、输出异常）都降级为全零默认值并记
错误日志——探测失败不该中断扫描循环；过滤条件会据此把文件筛掉或放行，
由用户配置决定。size 字段由调用方（扫描层）以 os.stat 结果覆盖，
ffprobe 的同名输出仅作占位。
"""

import json
import os
import subprocess
from typing import Any, TypedDict

from src.output.logger import Logger

# ffprobe 单次调用的超时：异常损坏的文件可能让 ffprobe 长时间无响应
_PROBE_TIMEOUT = 15


class MediaInfo(TypedDict):
    """媒体属性字典（过滤条件的求值输入）。"""

    duration: float
    size: int
    total_bitrate: float
    video_bitrate: float
    audio_bitrate: float
    framerate: float
    short_side: int
    video_codec: str
    audio_codec: str


def empty_media_info() -> MediaInfo:
    """全零默认值：ffprobe 未触发时（纯文件名/size 过滤）的求值输入。"""
    return {
        "duration": 0.0,
        "size": 0,
        "total_bitrate": 0.0,
        "video_bitrate": 0.0,
        "audio_bitrate": 0.0,
        "framerate": 0.0,
        "short_side": 0,
        "video_codec": "",
        "audio_codec": "",
    }


def _parse_framerate(stream: dict[str, Any]) -> float:
    """解析帧率字符串（"24000/1001" 或 "24/1"），优先 avg_frame_rate。"""
    fps_str = stream.get("avg_frame_rate") or stream.get("r_frame_rate", "")
    if not fps_str or "/" not in fps_str:
        return 0.0
    num, den = fps_str.split("/", 1)
    try:
        denominator = float(den)
        if denominator == 0:
            return 0.0
        return float(num) / denominator
    except ValueError:
        return 0.0


def _parse_streams(info: MediaInfo, streams: list[dict[str, Any]]) -> None:
    """只取第一个视频流与第一个音频流（与主流处理场景一致）。"""
    for stream in streams:
        codec_type = stream.get("codec_type")
        if codec_type == "video" and not info["video_codec"]:
            info["video_codec"] = stream.get("codec_name", "")
            if "bit_rate" in stream:
                try:
                    info["video_bitrate"] = float(stream["bit_rate"])
                except ValueError:
                    pass
            width = stream.get("width", 0)
            height = stream.get("height", 0)
            if width and height:
                info["short_side"] = min(width, height)
            info["framerate"] = _parse_framerate(stream)
        elif codec_type == "audio" and not info["audio_codec"]:
            info["audio_codec"] = stream.get("codec_name", "")
            if "bit_rate" in stream:
                try:
                    info["audio_bitrate"] = float(stream["bit_rate"])
                except ValueError:
                    pass


def get_media_info(filepath: str, logger: Logger) -> MediaInfo:
    """用 ffprobe 获取媒体文件属性；任何异常降级为全零默认值。"""
    info = empty_media_info()
    try:
        if os.path.exists(filepath):
            info["size"] = os.path.getsize(filepath)
    except OSError:
        pass

    cmd = [
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        filepath,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_PROBE_TIMEOUT, check=False
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)

            fmt = data.get("format", {})
            if "duration" in fmt:
                try:
                    info["duration"] = float(fmt["duration"])
                except ValueError:
                    pass
            if "bit_rate" in fmt:
                try:
                    info["total_bitrate"] = float(fmt["bit_rate"])
                except ValueError:
                    pass

            _parse_streams(info, data.get("streams", []))
    except Exception as e:  # noqa: BLE001
        # 兜底捕获：ffprobe 的任何异常（缺失、超时、输出异常）都不允许向上抛出，
        # 必须降级为全零默认值
        logger.error(f"获取 {filepath} 的媒体信息失败:\n{e}")

    return info
