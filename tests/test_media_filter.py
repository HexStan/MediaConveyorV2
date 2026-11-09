"""单元测试：文件过滤器（分类流水线、时间冷却与媒体属性匹配）。

钉死的语义（与 v1 MediaConveyor 一致）：
- include → exclude → passthrough 流水线，命中即停，exclude 优先于
  passthrough；include 非空且不匹配则跳过；
- fnmatch 匹配相对 source_dir 的路径，大小写不敏感，分隔符归一为 /；
- file_mtime 冷却恰等于阈值视为已冷却；
- passthrough 不参与媒体属性匹配，但仍受 file_mtime 约束。
"""

import pytest

from src.core.media.filter import FileFilter
from src.core.media.probe import empty_media_info
from src.core.task_config import FilterConfig
from src.core.types import FileAction


def make_filter(**overrides) -> FileFilter:
    config = FilterConfig.model_validate(overrides)
    return FileFilter(config)


class TestClassify:
    def test_no_patterns_processes_everything(self):
        f = make_filter()

        assert f.classify("video.mp4") is FileAction.PROCESS
        assert f.classify("sub/dir/video.mp4") is FileAction.PROCESS

    def test_include_filters_others(self):
        f = make_filter(include_patterns=["alpha/*", "bravo*/*", "*charlie*.mp4"])

        assert f.classify("alpha/a.mp4") is FileAction.PROCESS
        assert f.classify("alpha/deep/b.mp4") is FileAction.PROCESS
        assert f.classify("bravo-1/c.mp4") is FileAction.PROCESS
        assert f.classify("x/charlie/d.mp4") is FileAction.PROCESS
        assert f.classify("delta/e.mp4") is FileAction.SKIP

    def test_exclude_skips(self):
        f = make_filter(exclude_patterns=["*.tmp"])

        assert f.classify("a.tmp") is FileAction.SKIP
        assert f.classify("b.mp4") is FileAction.PROCESS

    def test_passthrough(self):
        f = make_filter(passthrough_patterns=["*.srt", "*.jpg"])

        assert f.classify("sub/a.srt") is FileAction.PASSTHROUGH
        assert f.classify("b.mp4") is FileAction.PROCESS

    def test_exclude_overrides_passthrough(self):
        f = make_filter(exclude_patterns=["*.tmp"], passthrough_patterns=["*"])

        assert f.classify("a.tmp") is FileAction.SKIP
        assert f.classify("b.srt") is FileAction.PASSTHROUGH

    def test_case_insensitive_and_separator_normalized(self):
        f = make_filter(include_patterns=["*.mp4"])

        assert f.classify("Sub/Dir/Video.MP4") is FileAction.PROCESS
        assert f.classify("video.txt") is FileAction.SKIP

    def test_windows_separator_normalized(self):
        f = make_filter(include_patterns=["alpha/*"])

        assert f.classify("alpha\\a.mp4") is FileAction.PROCESS

    def test_reserved_transient_names_skipped(self):
        """本工具瞬态文件为保留名：先于用户 patterns 判定，无条件跳过。"""
        f = make_filter(include_patterns=["*"], passthrough_patterns=["*"])

        assert f.classify(".mc-ffmpeg-part.a-encoded.mp4") is FileAction.SKIP
        assert f.classify("sub/.mc-ffmpeg-part.a-encoded.mp4") is FileAction.SKIP
        # 无扩展名的输出同样命中前缀标记
        assert f.classify(".mc-ffmpeg-part.a-encoded") is FileAction.SKIP
        assert f.classify(".state.json.mc-part") is FileAction.SKIP
        # 前缀匹配大小写不敏感（与 patterns 的归一规则一致）
        assert f.classify(".MC-FFMPEG-PART.a.mp4") is FileAction.SKIP
        # 普通文件不受影响：passthrough 全匹配下仍走直传
        assert f.classify("a.mp4") is FileAction.PASSTHROUGH


class TestCheckMtime:
    def test_zero_threshold_always_passes(self):
        f = make_filter()

        assert f.check_mtime(mtime=999999.0, current_time=1000.0) is True

    def test_old_enough_passes(self):
        f = make_filter(file_mtime=300)

        assert f.check_mtime(mtime=700.0, current_time=1000.0) is True

    def test_too_recent_fails(self):
        f = make_filter(file_mtime=300)

        assert f.check_mtime(mtime=900.0, current_time=1000.0) is False

    def test_exact_threshold_passes(self):
        """恰等于阈值视为已冷却。"""
        f = make_filter(file_mtime=300)

        assert f.check_mtime(mtime=700.0, current_time=1000.0) is True


class TestRequiresMediaInfo:
    def test_empty_filter(self):
        assert make_filter().requires_media_info() is False

    def test_size_alone_does_not_probe(self):
        assert make_filter(size={"min": "100MB"}).requires_media_info() is False

    def test_duration_requires_probe(self):
        assert make_filter(duration={"min": "10s"}).requires_media_info() is True

    def test_bitrate_requires_probe(self):
        assert make_filter(video_bitrate={"min": "500K"}).requires_media_info() is True
        assert make_filter(audio_bitrate={"max": "320K"}).requires_media_info() is True
        assert make_filter(total_bitrate={"min": "1M"}).requires_media_info() is True

    def test_framerate_and_short_side_require_probe(self):
        assert make_filter(framerate={"min": 24}).requires_media_info() is True
        assert make_filter(short_side={"min": 720}).requires_media_info() is True

    def test_codec_exclusion_requires_probe(self):
        assert make_filter(exclude_video_codecs=["hevc"]).requires_media_info() is True
        assert make_filter(exclude_audio_codecs=["opus"]).requires_media_info() is True


class TestMatch:
    @pytest.fixture
    def info(self):
        return {
            **empty_media_info(),
            "size": 1000,
            "duration": 60.0,
            "total_bitrate": 5_000_000.0,
            "video_bitrate": 3_000_000.0,
            "audio_bitrate": 192_000.0,
            "framerate": 30.0,
            "short_side": 1080,
            "video_codec": "h264",
            "audio_codec": "aac",
        }

    def test_no_conditions_matches(self, info, capture_log):
        assert make_filter().match(info, capture_log, "a.mp4") is True

    def test_size_min_max(self, capture_log):
        info = {**empty_media_info(), "size": 1000}

        assert make_filter(size={"min": "500B"}).match(info, capture_log, "a.mp4") is True
        assert make_filter(size={"min": "2KB"}).match(info, capture_log, "a.mp4") is False
        assert make_filter(size={"max": "2KB"}).match(info, capture_log, "a.mp4") is True
        assert make_filter(size={"max": "500B"}).match(info, capture_log, "a.mp4") is False
        assert capture_log.has("debug", "文件大小")

    def test_duration_bounds(self, info, capture_log):
        assert make_filter(duration={"min": "30s"}).match(info, capture_log, "a.mp4") is True
        assert make_filter(duration={"min": "120s"}).match(info, capture_log, "a.mp4") is False
        assert make_filter(duration={"max": "2m"}).match(info, capture_log, "a.mp4") is True
        assert make_filter(duration={"max": "30s"}).match(info, capture_log, "a.mp4") is False
        assert capture_log.has("debug", "时长")

    def test_bitrate_bounds(self, info, capture_log):
        assert make_filter(video_bitrate={"min": "1M"}).match(info, capture_log, "a.mp4") is True
        assert make_filter(video_bitrate={"min": "4M"}).match(info, capture_log, "a.mp4") is False
        assert make_filter(audio_bitrate={"max": "128K"}).match(info, capture_log, "a.mp4") is False
        assert make_filter(total_bitrate={"min": "6M"}).match(info, capture_log, "a.mp4") is False
        assert capture_log.has("debug", "视频码率")
        assert capture_log.has("debug", "音频码率")
        assert capture_log.has("debug", "总码率")

    def test_framerate_and_short_side(self, info, capture_log):
        assert make_filter(framerate={"min": 24}).match(info, capture_log, "a.mp4") is True
        assert make_filter(framerate={"min": 60}).match(info, capture_log, "a.mp4") is False
        assert make_filter(short_side={"min": 720}).match(info, capture_log, "a.mp4") is True
        assert make_filter(short_side={"min": 2160}).match(info, capture_log, "a.mp4") is False
        assert capture_log.has("debug", "帧率")
        assert capture_log.has("debug", "短边分辨率")

    def test_exclude_codecs_case_insensitive(self, info, capture_log):
        assert make_filter(exclude_video_codecs=["hevc"]).match(info, capture_log, "a.mp4") is True
        assert make_filter(exclude_video_codecs=["H264"]).match(info, capture_log, "a.mp4") is False
        assert make_filter(exclude_audio_codecs=["AAC"]).match(info, capture_log, "a.mp4") is False
        assert capture_log.has("debug", "视频编码")
        assert capture_log.has("debug", "音频编码")

    def test_zero_threshold_skips_dimension(self, info, capture_log):
        """阈值为 0 时不检查该维度（缺值维度不会被误判）。"""
        info_missing = {**empty_media_info(), "size": 1000}

        assert make_filter(size={"min": "1B"}).match(info_missing, capture_log, "a.mp4") is True
