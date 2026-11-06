"""单元测试：FFmpeg 任务配置模型（加载期校验收口的契约）。

钉死的契约：
- source_dir / dest_dir / ffmpeg_cmd / output_filename 必填，
  extra="forbid" 拒绝未知字段；
- output_filename 模板仅支持 {basename}，渲染结果必须是纯文件名；
- 数字字段接受数字字符串（lax 强转），布尔值一律拒绝；
- process_order 枚举字段大小写不敏感，缺省为 default，非法取值报
  中文错误；
- 过滤条件的范围值在加载期解析：非法值得到明确的中文错误，而不是
  运行期被静默当作 0；
- 配置了 fallback_count 必须同时配置备用命令。
"""

import pytest
from pydantic import ValidationError

from src.core.task_config import FilterConfig, TaskConfig
from src.validation import format_validation_error


def make_task(**overrides) -> dict:
    task = {
        "source_dir": "./source",
        "dest_dir": "./dest",
        "ffmpeg_cmd": "-i {input} {output}",
        "output_filename": "{basename}.mp4",
    }
    task.update(overrides)
    return task


class TestRequiredFields:
    def test_minimal_task(self):
        config = TaskConfig.model_validate(make_task())

        assert config.source_dir == "./source"
        assert config.dest_dir == "./dest"
        assert config.output_filename == "{basename}.mp4"
        assert config.failure_count == 3
        assert config.fallback_count == 0
        assert config.ffmpeg_timeout == 0
        assert config.stable_duration == 0
        assert config.process_order == "default"
        assert config.remove_source is False
        assert config.source_expired_minutes == 0
        assert config.backup_dir == ""
        assert config.ffmpeg_cmd_fallback == ""

    @pytest.mark.parametrize("field", ["source_dir", "dest_dir", "ffmpeg_cmd", "output_filename"])
    def test_missing_required_field_rejected(self, field):
        task = make_task()
        del task[field]

        with pytest.raises(ValidationError) as exc_info:
            TaskConfig.model_validate(task)

        message = format_validation_error(exc_info.value)
        assert f"{field}: 缺少必填字段" in message


class TestFieldCoercion:
    def test_numeric_strings_coerced(self):
        config = TaskConfig.model_validate(
            make_task(failure_count="5", stable_duration="10", source_expired_minutes="60")
        )

        assert config.failure_count == 5
        assert config.stable_duration == 10.0
        assert config.source_expired_minutes == 60.0

    def test_bool_rejected_for_numeric_fields(self):
        with pytest.raises(ValidationError, match="不接受布尔值"):
            TaskConfig.model_validate(make_task(failure_count=True))

        with pytest.raises(ValidationError, match="不接受布尔值"):
            TaskConfig.model_validate(make_task(stable_duration=False))

    def test_negative_values_rejected(self):
        with pytest.raises(ValidationError):
            TaskConfig.model_validate(make_task(failure_count=-1))

        with pytest.raises(ValidationError):
            TaskConfig.model_validate(make_task(ffmpeg_timeout=-5))

    def test_unknown_fields_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            TaskConfig.model_validate(make_task(ffmpeg_command="x"))

        assert "ffmpeg_command: 不支持的字段" in format_validation_error(exc_info.value)

    def test_multiline_cmd_preserved(self):
        config = TaskConfig.model_validate(make_task(ffmpeg_cmd="-i {input} \\\n  {output}"))

        assert "\n" in config.ffmpeg_cmd


class TestOutputFilenameValidation:
    """output_filename 的加载期校验：占位符支持项与纯文件名形状。"""

    def test_template_preserved(self):
        config = TaskConfig.model_validate(make_task(output_filename="{basename}-encoded.mp4"))

        assert config.output_filename == "{basename}-encoded.mp4"

    def test_unknown_placeholder_rejected(self):
        with pytest.raises(ValidationError, match="output_filename 模板"):
            TaskConfig.model_validate(make_task(output_filename="{output}.mp4"))

    def test_empty_braces_rejected(self):
        with pytest.raises(ValidationError, match="output_filename 模板"):
            TaskConfig.model_validate(make_task(output_filename="{}.mp4"))

    def test_path_separators_rejected(self):
        with pytest.raises(ValidationError, match="纯文件名"):
            TaskConfig.model_validate(make_task(output_filename="sub/{basename}.mp4"))

        with pytest.raises(ValidationError, match="纯文件名"):
            TaskConfig.model_validate(make_task(output_filename="..\\{basename}.mp4"))

    def test_dot_names_rejected(self):
        with pytest.raises(ValidationError, match="纯文件名"):
            TaskConfig.model_validate(make_task(output_filename=".."))


class TestFallbackValidation:
    def test_fallback_without_cmd_rejected(self):
        with pytest.raises(ValidationError, match="必须同时配置 ffmpeg_cmd_fallback"):
            TaskConfig.model_validate(make_task(fallback_count=3))

    def test_fallback_with_cmd_accepted(self):
        config = TaskConfig.model_validate(
            make_task(fallback_count=3, ffmpeg_cmd_fallback="ffmpeg -fallback -i {input} {output}")
        )

        assert config.fallback_count == 3


class TestProcessOrder:
    """process_order 枚举字段：缺省值、大小写归一与非法取值报错。"""

    @pytest.mark.parametrize(
        "value",
        [
            "default",
            "alphabetic",
            "largest_first",
            "smallest_first",
            "newest_first",
            "oldest_first",
        ],
    )
    def test_valid_values_accepted(self, value):
        config = TaskConfig.model_validate(make_task(process_order=value))

        assert config.process_order == value

    def test_value_case_insensitive(self):
        config = TaskConfig.model_validate(make_task(process_order="Largest_First"))

        assert config.process_order == "largest_first"

    def test_invalid_value_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            TaskConfig.model_validate(make_task(process_order="random"))

        message = format_validation_error(exc_info.value)
        assert "process_order: 配置值无效，允许的取值" in message

    def test_non_string_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            TaskConfig.model_validate(make_task(process_order=123))

        assert "process_order: 配置值无效，允许的取值" in format_validation_error(exc_info.value)


class TestFilterConfig:
    def test_defaults(self):
        config = FilterConfig.model_validate({})

        assert config.include_patterns == []
        assert config.exclude_patterns == []
        assert config.passthrough_patterns == []
        assert config.file_mtime == 0
        assert config.size.min == 0
        assert config.size.max == 0

    def test_patterns_lowered(self):
        config = FilterConfig.model_validate(
            {"include_patterns": ["*.MP4", "Sub/*.MKV"], "exclude_video_codecs": ["HEVC"]}
        )

        assert config.include_patterns == ["*.mp4", "sub/*.mkv"]
        assert config.exclude_video_codecs == ["hevc"]

    def test_patterns_type_error_translated(self):
        with pytest.raises(ValidationError) as exc_info:
            FilterConfig.model_validate({"include_patterns": "not-a-list"})

        assert "应为数组结构" in format_validation_error(exc_info.value)

    def test_size_range_parsed_to_bytes(self):
        config = FilterConfig.model_validate({"size": {"min": "100MB", "max": "20GiB"}})

        assert config.size.min == 100_000_000
        assert config.size.max == 20 * 2**30

    def test_size_range_numeric_values_are_bytes(self):
        config = FilterConfig.model_validate({"size": {"min": 4096}})

        assert config.size.min == 4096

    def test_invalid_size_value_fails_loudly(self):
        """v1 的静默当 0 缺陷已修复：非法值在加载期得到明确错误。"""
        with pytest.raises(ValidationError, match="无法解析"):
            FilterConfig.model_validate({"size": {"min": "abc"}})

    def test_string_range_form_rejected(self):
        """v1 的 `size = "100MB"` 字符串写法（被静默当 0）现在明确报错。"""
        with pytest.raises(ValidationError) as exc_info:
            FilterConfig.model_validate({"size": "100MB"})

        assert "应为字典结构" in format_validation_error(exc_info.value)

    def test_duration_range_parsed_to_seconds(self):
        config = FilterConfig.model_validate({"duration": {"min": "10s", "max": "2h"}})

        assert config.duration.min == 10.0
        assert config.duration.max == 7200.0

    def test_invalid_duration_value_fails_loudly(self):
        with pytest.raises(ValidationError, match="无法解析"):
            FilterConfig.model_validate({"duration": {"min": "ten seconds"}})

    def test_bitrate_range_parsed_to_bps(self):
        config = FilterConfig.model_validate(
            {"video_bitrate": {"min": "500K"}, "audio_bitrate": {"max": "320Kbps"}}
        )

        assert config.video_bitrate.min == 500_000.0
        assert config.audio_bitrate.max == 320_000.0

    def test_framerate_and_short_side(self):
        config = FilterConfig.model_validate(
            {"framerate": {"min": 24, "max": "60"}, "short_side": {"min": "720"}}
        )

        assert config.framerate.min == 24.0
        assert config.framerate.max == 60.0
        assert config.short_side.min == 720

    def test_file_mtime_coerced(self):
        config = FilterConfig.model_validate({"file_mtime": "300"})

        assert config.file_mtime == 300.0

    def test_filter_embedded_in_task(self):
        config = TaskConfig.model_validate(
            make_task(filter={"include_patterns": ["*.mp4"], "file_mtime": 300})
        )

        assert config.filter.include_patterns == ["*.mp4"]
        assert config.filter.file_mtime == 300.0
