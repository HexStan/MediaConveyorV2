"""单元测试：FFmpeg progress 协议块的解析与中文进度行渲染。

块解析与渲染语义从 execute() 入口观察成本畸高（进行中行被 30 秒
节流压制、字段组合众多），只能白盒直测纯函数（tests/README.md
原则二）。
"""

from src.core.media.ffmpeg.progress import (
    FFmpegProgress,
    format_progress,
    parse_progress_block,
)


class TestParseProgressBlock:
    def test_full_block(self):
        progress = parse_progress_block(
            [
                "frame=168",
                "fps=42.13",
                "bitrate= 512.3kbits/s",
                "total_size=1052204",
                "out_time=00:00:04.200000",
                "dup_frames=0",
                "drop_frames=0",
                "speed=1.05x",
                "progress=continue",
            ]
        )

        assert progress == FFmpegProgress(
            progress="continue",
            frame="168",
            fps="42.13",
            bitrate="512.3kbits/s",
            total_size=1052204,
            out_time="00:00:04.200000",
            dup_frames="0",
            drop_frames="0",
            speed="1.05x",
        )

    def test_missing_progress_key_returns_none(self):
        """无 progress 键的内容不构成块（块的收尾标志缺失）。"""
        assert parse_progress_block(["frame=1", "fps=1.0"]) is None

    def test_total_size_na_parses_to_none(self):
        progress = parse_progress_block(["total_size=N/A", "progress=end"])

        assert progress is not None
        assert progress.total_size is None

    def test_missing_fields_default_empty(self):
        progress = parse_progress_block(["progress=end"])

        assert progress is not None
        assert progress.frame == ""
        assert progress.out_time == ""


class TestFormatProgress:
    def test_full_line(self):
        progress = FFmpegProgress(
            progress="continue",
            frame="168",
            fps="42.13",
            bitrate="512.3kbits/s",
            total_size=1052204,
            out_time="00:00:04.200000",
            dup_frames="0",
            drop_frames="0",
            speed="1.05x",
        )

        assert format_progress(progress, elapsed=12.4, total_duration=8.4) == (
            "进行中 | 持续时间：00:12 / 00:12 | 输出时间戳：00:04 / 00:08 | 当前帧：168 "
            "| 码率：512.3kbits/s | 体积：1.0 MiB | 帧率：42.13 | 速率：1.05x "
            "| 复制帧：0 | 丢弃帧：0"
        )

    def test_end_state_and_missing_fields(self):
        """end 汉化为已完成；N/A 与缺失字段统一为占位符；ETA 恒为 00:00。"""
        progress = FFmpegProgress(
            progress="end",
            frame="",
            fps="",
            bitrate="",
            total_size=None,
            out_time="N/A",
            dup_frames="",
            drop_frames="",
            speed="",
        )

        assert format_progress(progress, elapsed=0.4, total_duration=None) == (
            "已完成 | 持续时间：00:00 / 00:00 | 输出时间戳：-- / -- | 当前帧：-- | 码率：-- "
            "| 体积：-- | 帧率：-- | 速率：-- | 复制帧：-- | 丢弃帧：--"
        )

    def test_eta_placeholder_when_duration_unknown(self):
        """总时长未知（启动横幅未解析到）：ETA 为占位符。"""
        progress = parse_progress_block(["out_time=00:00:04.200000", "progress=continue"])

        assert progress is not None
        line = format_progress(progress, elapsed=12.4, total_duration=None)
        assert "持续时间：00:12 / --" in line

    def test_eta_placeholder_before_first_output(self):
        """out_time 尚未产出（缺失 / N/A / 为零 / 负起始偏移）：占位符。"""
        for out_time in ("", "N/A", "00:00:00.000000", "-00:00:00.020000"):
            progress = parse_progress_block([f"out_time={out_time}", "progress=continue"])

            assert progress is not None
            line = format_progress(progress, elapsed=12.4, total_duration=8.4)
            assert "持续时间：00:12 / --" in line, out_time

    def test_eta_clamps_to_zero_when_out_time_exceeds_duration(self):
        """out_time 超过总时长（非零起始偏移等）：剩余钳为 0。"""
        progress = parse_progress_block(["out_time=00:00:08.400000", "progress=continue"])

        assert progress is not None
        line = format_progress(progress, elapsed=12.4, total_duration=4.2)
        assert "持续时间：00:12 / 00:00" in line

    def test_timer_clamps_negative_elapsed(self):
        progress = parse_progress_block(["progress=continue"])

        assert progress is not None
        assert "持续时间：00:00" in format_progress(progress, elapsed=-1.0, total_duration=None)

    def test_minutes_unbounded_beyond_one_hour(self):
        """分钟位不封顶：超过 1 小时不进位小时位，分钟照常累加（62:05）。"""
        progress = parse_progress_block(["out_time=01:02:03.000000", "progress=continue"])

        assert progress is not None
        line = format_progress(progress, elapsed=3725.0, total_duration=None)
        assert "持续时间：62:05" in line
        assert "输出时间戳：62:03 / --" in line

    def test_total_duration_beyond_one_hour(self):
        """总时长同样分钟位不封顶：作为输出时间戳的分母显示。"""
        progress = parse_progress_block(["out_time=00:30:00.000000", "progress=continue"])

        assert progress is not None
        line = format_progress(progress, elapsed=60.0, total_duration=3725.0)
        assert "输出时间戳：30:00 / 62:05" in line

    def test_out_time_negative_offset_keeps_sign(self):
        """负起始偏移的 out_time：显示保留符号。"""
        progress = parse_progress_block(["out_time=-00:00:00.020000", "progress=continue"])

        assert progress is not None
        line = format_progress(progress, elapsed=12.4, total_duration=None)
        assert "输出时间戳：-00:00 / --" in line
