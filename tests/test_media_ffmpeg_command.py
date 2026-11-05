"""单元测试：FFmpeg 命令组装（模板填充、程序预置参数、part 文件命名）。

命令模板的填充结果、占位符报错与引号规则从 execute() 入口无法观察
（tests/README.md 原则二），只能白盒直测纯函数。引号断言以
posix_shell fixture 与运行平台解耦。
"""

import sys

import pytest

from src.core.media.ffmpeg.command import (
    TemplateFormatError,
    build_command,
    part_output_filename,
    render_output_filename,
)


@pytest.fixture
def posix_shell(monkeypatch):
    """引号断言与运行平台解耦：统一钉死为 POSIX（shlex 单引号规则）。"""
    monkeypatch.setattr(sys, "platform", "linux")


class TestBuildCommand:
    def test_preset_args_injected_before_template(self, posix_shell):
        """组装顺序：ffmpeg 可执行文件 → 程序预置参数 → 用户参数模板。"""
        cmd = build_command("-i {input} {output}", "/src/a.mp4", "/dest/a.mp4")

        assert cmd == "ffmpeg -y -nostdin -nostats -progress pipe:1 -i /src/a.mp4 /dest/a.mp4"

    def test_quotes_unsafe_values(self, posix_shell):
        cmd = build_command("-i {input} {output}", "/src/a b.mp4", "/dest/a b.mp4")

        assert (
            cmd == "ffmpeg -y -nostdin -nostats -progress pipe:1 -i '/src/a b.mp4' '/dest/a b.mp4'"
        )

    def test_safe_values_stay_unquoted(self, posix_shell):
        cmd = build_command("-i {input} {output}", "/src/a.mp4", "/dest/a.mp4")

        assert cmd == "ffmpeg -y -nostdin -nostats -progress pipe:1 -i /src/a.mp4 /dest/a.mp4"

    def test_windows_values_double_quoted(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        cmd = build_command("-i {input} {output}", "C:\\src\\a b.mp4", "C:\\dest\\a b.mp4")

        assert cmd == (
            'ffmpeg -y -nostdin -nostats -progress pipe:1 -i "C:\\src\\a b.mp4" "C:\\dest\\a b.mp4"'
        )

    def test_windows_trailing_backslash_escaped(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        cmd = build_command("-i {input}", "C:\\src\\", "C:\\out")

        assert cmd == 'ffmpeg -y -nostdin -nostats -progress pipe:1 -i "C:\\src\\\\"'

    def test_multiline_merged_to_single_line(self, posix_shell):
        cmd = build_command("-i {input} \\\n  {output}", "/in a.mp4", "/out b.mp4")

        assert "\n" not in cmd
        assert "\r" not in cmd
        assert "-i '/in a.mp4'" in cmd

    def test_unknown_placeholder_raises(self, posix_shell):
        with pytest.raises(TemplateFormatError, match="占位符"):
            build_command("{oops}", "/in", "/out")

    def test_empty_braces_raise(self, posix_shell):
        with pytest.raises(TemplateFormatError):
            build_command("{}", "/in", "/out")


class TestRenderOutputFilename:
    def test_renders_basename(self):
        assert render_output_filename("{basename}-encoded.mp4", "a b") == "a b-encoded.mp4"

    def test_basename_expandable_multiple_times(self):
        assert render_output_filename("{basename}-{basename}.mp4", "a") == "a-a.mp4"

    def test_unknown_placeholder_raises(self):
        with pytest.raises(TemplateFormatError, match="basename"):
            render_output_filename("{output}.mp4", "a")

    def test_empty_braces_raise(self):
        with pytest.raises(TemplateFormatError):
            render_output_filename("{}", "a")


class TestPartOutputFilename:
    def test_part_name_wraps_final_name(self):
        assert part_output_filename("a-encoded.mp4") == ".mc-ffmpeg-part.a-encoded.mp4"
