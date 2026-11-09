"""单元测试：ffprobe 封装（进程边界打桩，钉死解析与容错契约）。

ffprobe 的任何异常（缺失、超时、输出异常）都必须降级为全零默认值并
记错误日志，不允许向上抛——探测失败不中断扫描循环。
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from src.core.media.probe import empty_media_info, get_media_info

FFPROBE_OUTPUT = {
    "format": {
        "duration": "120.5",
        "bit_rate": "5000000",
    },
    "streams": [
        {
            "codec_type": "video",
            "codec_name": "h264",
            "bit_rate": "3000000",
            "width": 1920,
            "height": 1080,
            "avg_frame_rate": "30/1",
        },
        {
            "codec_type": "audio",
            "codec_name": "aac",
            "bit_rate": "192000",
        },
    ],
}


@pytest.fixture
def media_file(tmp_path):
    path = tmp_path / "test.mp4"
    path.write_bytes(b"\x00" * 1000)
    return str(path)


def _mock_run(stdout="", returncode=0, side_effect=None):
    mock_result = MagicMock()
    mock_result.returncode = returncode
    mock_result.stdout = stdout
    return patch("subprocess.run", return_value=mock_result, side_effect=side_effect)


class TestGetMediaInfo:
    def test_parses_full_ffprobe_output(self, media_file, capture_log):
        with _mock_run(stdout=json.dumps(FFPROBE_OUTPUT)):
            info = get_media_info(media_file, capture_log)

        assert info["duration"] == 120.5
        assert info["size"] == 1000
        assert info["total_bitrate"] == 5000000.0
        assert info["video_bitrate"] == 3000000.0
        assert info["audio_bitrate"] == 192000.0
        assert info["framerate"] == 30.0
        assert info["short_side"] == 1080
        assert info["video_codec"] == "h264"
        assert info["audio_codec"] == "aac"
        assert not capture_log.has("error", "ffprobe")

    def test_missing_file_yields_defaults(self, capture_log):
        with patch("os.path.exists", return_value=False):
            info = get_media_info("/nonexistent/file.mp4", capture_log)

        assert info == empty_media_info()

    def test_missing_duration_field(self, media_file, capture_log):
        output = {"format": {}, "streams": []}
        with _mock_run(stdout=json.dumps(output)):
            info = get_media_info(media_file, capture_log)

        assert info["duration"] == 0.0
        assert info["size"] == 1000

    def test_ffprobe_exception_degrades_to_defaults(self, media_file, capture_log):
        with _mock_run(side_effect=Exception("ffprobe not found")):
            info = get_media_info(media_file, capture_log)

        assert info["size"] == 1000
        assert info["duration"] == 0.0
        assert capture_log.has("error", "的媒体信息失败")

    def test_nonzero_return_code(self, media_file, capture_log):
        with _mock_run(returncode=1):
            info = get_media_info(media_file, capture_log)

        assert info["duration"] == 0.0
        assert info["size"] == 1000

    def test_invalid_duration_string(self, media_file, capture_log):
        output = {"format": {"duration": "not_a_number"}, "streams": []}
        with _mock_run(stdout=json.dumps(output)):
            info = get_media_info(media_file, capture_log)

        assert info["duration"] == 0.0

    def test_invalid_bit_rate_string(self, media_file, capture_log):
        output = {"format": {"bit_rate": "invalid"}, "streams": []}
        with _mock_run(stdout=json.dumps(output)):
            info = get_media_info(media_file, capture_log)

        assert info["total_bitrate"] == 0.0

    def test_framerate_r_frame_rate_fallback(self, media_file, capture_log):
        output = {
            "format": {},
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "r_frame_rate": "24000/1001",
                    "width": 1920,
                    "height": 1080,
                }
            ],
        }
        with _mock_run(stdout=json.dumps(output)):
            info = get_media_info(media_file, capture_log)

        assert info["framerate"] == pytest.approx(24000 / 1001, rel=1e-3)

    def test_framerate_zero_denominator(self, media_file, capture_log):
        output = {
            "format": {},
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "avg_frame_rate": "24/0",
                    "width": 1920,
                    "height": 1080,
                }
            ],
        }
        with _mock_run(stdout=json.dumps(output)):
            info = get_media_info(media_file, capture_log)

        assert info["framerate"] == 0.0

    def test_framerate_without_slash(self, media_file, capture_log):
        output = {
            "format": {},
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "avg_frame_rate": "30",
                    "width": 1920,
                    "height": 1080,
                }
            ],
        }
        with _mock_run(stdout=json.dumps(output)):
            info = get_media_info(media_file, capture_log)

        assert info["framerate"] == 0.0

    def test_multiple_streams_first_takes_priority(self, media_file, capture_log):
        output = {
            "format": {},
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1920,
                    "height": 1080,
                },
                {
                    "codec_type": "video",
                    "codec_name": "mpeg4",
                    "width": 720,
                    "height": 480,
                },
                {"codec_type": "audio", "codec_name": "aac", "bit_rate": "128000"},
                {"codec_type": "audio", "codec_name": "mp3", "bit_rate": "192000"},
            ],
        }
        with _mock_run(stdout=json.dumps(output)):
            info = get_media_info(media_file, capture_log)

        assert info["video_codec"] == "h264"
        assert info["short_side"] == 1080
        assert info["audio_codec"] == "aac"
        assert info["audio_bitrate"] == 128000.0

    def test_getsize_oserror_handled(self, capture_log):
        with (
            patch("os.path.exists", return_value=True),
            patch("os.path.getsize", side_effect=OSError("permission denied")),
            _mock_run(side_effect=Exception("skip ffprobe")),
        ):
            info = get_media_info("/fake/file.mp4", capture_log)

        assert info["size"] == 0


class TestEmptyMediaInfo:
    def test_all_dimensions_zeroed(self):
        info = empty_media_info()

        assert info["duration"] == 0.0
        assert info["size"] == 0
        assert info["total_bitrate"] == 0.0
        assert info["video_bitrate"] == 0.0
        assert info["audio_bitrate"] == 0.0
        assert info["framerate"] == 0.0
        assert info["short_side"] == 0
        assert info["video_codec"] == ""
        assert info["audio_codec"] == ""


class TestProbeCommand:
    def test_probe_uses_quiet_json_mode(self, media_file, capture_log):
        with _mock_run(stdout="{}") as mock:
            get_media_info(media_file, capture_log)

        cmd = mock.call_args.args[0]
        assert cmd[:6] == ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format"]
        assert "-show_streams" in cmd
        assert cmd[-1] == media_file

    def test_probe_timeout_is_bounded(self, capture_log):
        with _mock_run(stdout="{}") as mock:
            get_media_info("/fake/file.mp4", capture_log)

        assert mock.call_args.kwargs["timeout"] == 15
