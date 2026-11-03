"""文件过滤：文件名分类流水线与媒体属性匹配。

分类流水线（命中即停）：include（不匹配则跳过）→ exclude（匹配则跳过）
→ passthrough（匹配则直传）→ 默认走 FFmpeg 处理。三个 pattern 列表均以 fnmatch
匹配相对 source_dir 的路径，匹配前统一小写并以 / 归一路径分隔符，
等效大小写不敏感。

媒体属性匹配仅在配置了需要 ffprobe 的条件时执行（requires_media_info）；
size 与 file_mtime 不触发 ffprobe，纯文件名/大小/时间过滤的扫描速度
不受影响。所有匹配失败都打 debug 日志说明跳过原因，便于排查。
"""

import fnmatch
from collections.abc import Sequence

from src.core.media.probe import MediaInfo
from src.core.task_config import FilterConfig
from src.core.types import FileAction
from src.output.logger import Logger


class FileFilter:
    """运行期过滤器：消费加载期已解析的 FilterConfig（阈值均为数值）。"""

    def __init__(self, config: FilterConfig) -> None:
        self._config = config

    # ---- 文件名分类 -----------------------------------------------------

    @staticmethod
    def _match_patterns(rel_path: str, patterns: Sequence[str]) -> bool:
        return any(fnmatch.fnmatchcase(rel_path, p) for p in patterns)

    def classify(self, rel_path: str) -> FileAction:
        """根据相对路径判断文件分类。

        输入路径会先做小写与分隔符归一（与 patterns 加载期的归一一致）。
        """
        rel_path = rel_path.replace("\\", "/").lower()
        config = self._config

        if config.include_patterns and not self._match_patterns(rel_path, config.include_patterns):
            return FileAction.SKIP
        if self._match_patterns(rel_path, config.exclude_patterns):
            return FileAction.SKIP
        if self._match_patterns(rel_path, config.passthrough_patterns):
            return FileAction.PASSTHROUGH
        return FileAction.PROCESS

    # ---- 时间冷却 -------------------------------------------------------

    def check_mtime(self, mtime: float, current_time: float) -> bool:
        """检查文件修改时间是否满足冷却阈值（恰等于阈值视为已冷却）。"""
        threshold = self._config.file_mtime
        if threshold <= 0:
            return True
        return (current_time - mtime) >= threshold

    # ---- 媒体属性匹配 ---------------------------------------------------

    def requires_media_info(self) -> bool:
        """是否配置了需要 ffprobe 才能求值的条件（size/mtime 以外）。"""
        config = self._config
        return any(
            [
                config.duration.min > 0,
                config.duration.max > 0,
                config.total_bitrate.min > 0,
                config.total_bitrate.max > 0,
                config.video_bitrate.min > 0,
                config.video_bitrate.max > 0,
                config.audio_bitrate.min > 0,
                config.audio_bitrate.max > 0,
                config.framerate.min > 0,
                config.framerate.max > 0,
                config.short_side.min > 0,
                config.short_side.max > 0,
                bool(config.exclude_video_codecs),
                bool(config.exclude_audio_codecs),
            ]
        )

    def match(self, media_info: MediaInfo, logger: Logger, rel_path: str) -> bool:
        """逐维度求值媒体属性过滤条件，任一不满足即 False（命中即停）。

        维度求值顺序与配置文档一致：size → duration → total_bitrate →
        video_bitrate → audio_bitrate → framerate → short_side → 编码排除。
        阈值 0 表示该边界不限制。
        """
        config = self._config
        # (最小阈值, 最大阈值, 实际值, 日志标签)
        dimensions: tuple[tuple[float, float, float, str], ...] = (
            (config.size.min, config.size.max, float(media_info["size"]), "文件大小"),
            (config.duration.min, config.duration.max, media_info["duration"], "时长"),
            (
                config.total_bitrate.min,
                config.total_bitrate.max,
                media_info["total_bitrate"],
                "总码率",
            ),
            (
                config.video_bitrate.min,
                config.video_bitrate.max,
                media_info["video_bitrate"],
                "视频码率",
            ),
            (
                config.audio_bitrate.min,
                config.audio_bitrate.max,
                media_info["audio_bitrate"],
                "音频码率",
            ),
            (config.framerate.min, config.framerate.max, media_info["framerate"], "帧率"),
            (
                config.short_side.min,
                config.short_side.max,
                float(media_info["short_side"]),
                "短边分辨率",
            ),
        )
        for dim_min, dim_max, actual, label in dimensions:
            if dim_min > 0 and actual < dim_min:
                logger.debug(f"跳过 {rel_path}，原因: {label} ({actual}) 小于最小值 ({dim_min})")
                return False
            if dim_max > 0 and actual > dim_max:
                logger.debug(f"跳过 {rel_path}，原因: {label} ({actual}) 大于最大值 ({dim_max})")
                return False

        video_codec = media_info["video_codec"]
        if config.exclude_video_codecs and video_codec.lower() in config.exclude_video_codecs:
            logger.debug(f"跳过 {rel_path}，原因: 视频编码 ({video_codec}) 在排除列表中")
            return False

        audio_codec = media_info["audio_codec"]
        if config.exclude_audio_codecs and audio_codec.lower() in config.exclude_audio_codecs:
            logger.debug(f"跳过 {rel_path}，原因: 音频编码 ({audio_codec}) 在排除列表中")
            return False

        return True
