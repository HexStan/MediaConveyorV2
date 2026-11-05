"""单元测试：FFmpeg 命令构建、progress 协议消费与进程托管执行。

Popen 是进程边界允许打桩的接缝（tests/README.md 原则三，桩见 conftest
的 stub_ffmpeg）：FakePopen 模拟 ffmpeg 的输出流（stderr 错误输出 +
stdout progress 块）与退出码，钉死命令组装（模板填充与预置参数注入）、
progress 块的解析渲染、进度行节流与 end 终写时序、超时终止与 fast 中止
的契约。FFmpeg 语义本身不测。
"""

import io
import subprocess
import sys

import pytest

from src.app.lifecycle import LifecycleCoordinator
from src.core.media.ffmpeg import (
    FFmpegProgress,
    TemplateFormatError,
    build_command,
    format_progress,
    parse_progress_block,
    part_output_filename,
    render_output_filename,
    run_ffmpeg,
)
from src.fs import TransferAbortedError


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


def _continue_block() -> bytes:
    return (
        b"frame=10\nfps=2.0\nbitrate=128.0kbits/s\ntotal_size=2048\n"
        b"out_time=00:00:01.000000\ndup_frames=0\ndrop_frames=0\nspeed=2.0x\n"
        b"progress=continue\n"
    )


def _end_block() -> bytes:
    return (
        b"frame=20\nfps=2.0\nbitrate=128.0kbits/s\ntotal_size=4096\n"
        b"out_time=00:00:02.000000\ndup_frames=0\ndrop_frames=0\nspeed=2.0x\n"
        b"progress=end\n"
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

        assert format_progress(progress, elapsed=12.4) == (
            "【进行中】 持续时间：00:00:12 | 输出时间戳：00:00:04 | 当前帧：168 "
            "| 码率：512.3kbits/s | 体积：1.0 MiB | 帧率：42.13 | 速率：1.05x "
            "| 复制帧：0 | 丢弃帧：0"
        )

    def test_end_state_and_missing_fields(self):
        """end 汉化为已完成；N/A 与缺失字段统一为占位符。"""
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

        assert format_progress(progress, elapsed=0.4) == (
            "【已完成】 持续时间：00:00:00 | 输出时间戳：-- | 当前帧：-- | 码率：-- "
            "| 体积：-- | 帧率：-- | 速率：-- | 复制帧：-- | 丢弃帧：--"
        )

    def test_timer_clamps_negative_elapsed(self):
        progress = parse_progress_block(["progress=continue"])

        assert progress is not None
        assert "持续时间：00:00:00" in format_progress(progress, elapsed=-1.0)


class TestRunFFmpeg:
    def test_success_appends_end_progress_record(self, stub_ffmpeg, capture_log):
        """成功结束：以 progress=end 快照追加一次终写状态行，无「运行报告」。"""
        _calls, state = stub_ffmpeg
        state["progress"] = _continue_block() + _end_block()

        ok = run_ffmpeg("ffmpeg cmd", logger=capture_log, coordinator=LifecycleCoordinator())

        assert ok is True
        assert capture_log.has("info", "FFmpeg 处理完成")
        # 运行报告行取消：终态由 progress=end 的终写进度行承载
        assert not any("运行报告" in m for _, m in capture_log.messages)
        finished = [m for lv, m in capture_log.messages if lv == "info" and "【已完成】" in m]
        assert len(finished) == 1

    def test_stderr_stats_lines_are_plain_error_output(self, stub_ffmpeg, capture_log):
        """-nostats 后 stderr 不再有进度语义：time=/speed= 行只是普通输出行。"""
        _calls, state = stub_ffmpeg
        state["output"] = b"time=00:00:01.00 speed=2.0x\r\n"

        run_ffmpeg("ffmpeg cmd", logger=capture_log, coordinator=LifecycleCoordinator())

        assert not capture_log.has("info", "FFmpeg 进度")
        assert not capture_log.has("info", "【进行中】")
        assert not capture_log.has("info", "【已完成】")

    def test_progress_lines_throttled(self, stub_ffmpeg, capture_log):
        """进行中行 30 秒节流：首块即落笔，其后毫秒级抵达的块被压制；异常结束不终写。"""
        _calls, state = stub_ffmpeg
        state["hangs"] = True
        state["progress"] = _continue_block() * 3

        ok = run_ffmpeg(
            "ffmpeg cmd",
            logger=capture_log,
            coordinator=LifecycleCoordinator(),
            timeout=0.3,
        )

        assert ok is False
        assert capture_log.has("error", "处理时长超出")
        progress_logs = [m for lv, m in capture_log.messages if lv == "info" and "【进行中】" in m]
        assert len(progress_logs) == 1
        assert not capture_log.has("info", "【已完成】")

    def test_progress_lines_log_every_poll_when_throttle_disabled(
        self, stub_ffmpeg, capture_log, monkeypatch
    ):
        """节流间隔归零（测试替身）：无新块也以最新快照落笔，计时器持续走动。"""
        monkeypatch.setattr("src.core.media.ffmpeg._PROGRESS_LOG_INTERVAL", 0)
        _calls, state = stub_ffmpeg
        state["hangs"] = True
        state["progress"] = _continue_block()

        ok = run_ffmpeg(
            "ffmpeg cmd",
            logger=capture_log,
            coordinator=LifecycleCoordinator(),
            timeout=1.2,
        )

        assert ok is False
        progress_logs = [m for lv, m in capture_log.messages if lv == "info" and "【进行中】" in m]
        assert len(progress_logs) >= 2

    def test_failure_appends_no_end_record(self, stub_ffmpeg, capture_log):
        """命令失败（无 end 块）：不追加终写状态行，最后一行进度原样保留。"""
        _calls, state = stub_ffmpeg
        state["returncode"] = 1
        state["progress"] = _continue_block()

        ok = run_ffmpeg("ffmpeg cmd", logger=capture_log, coordinator=LifecycleCoordinator())

        assert ok is False
        assert capture_log.has("info", "【进行中】")
        assert not capture_log.has("info", "【已完成】")

    def test_failure_logs_error_tail(self, stub_ffmpeg, capture_log):
        _calls, state = stub_ffmpeg
        state["returncode"] = 1
        state["output"] = b"\n".join([b"line %d" % i for i in range(30)]) + b"\n"

        coordinator = LifecycleCoordinator()
        ok = run_ffmpeg("ffmpeg cmd", logger=capture_log, coordinator=coordinator)

        assert ok is False
        # 只保留末尾 20 行
        error_msg = next(m for lv, m in capture_log.messages if lv == "error")
        assert "line 29" in error_msg
        assert "line 5" not in error_msg

    def test_process_registered_during_run(self, stub_ffmpeg, capture_log):
        """子进程在执行期间登记到协调器，结束后摘除（fast 停止整树终止的前提）。"""
        _calls, _state = stub_ffmpeg
        registered: list[int] = []

        coordinator = LifecycleCoordinator()
        original_register = coordinator.register_process

        def register(process):
            registered.append(process.pid)
            original_register(process)

        coordinator.register_process = register  # type: ignore[method-assign]
        run_ffmpeg(
            "ffmpeg cmd",
            logger=capture_log,
            coordinator=coordinator,
        )

        assert 4321 in registered
        assert coordinator._children == []

    def test_timeout_terminates_and_fails(self, stub_ffmpeg, capture_log, monkeypatch):
        _calls, state = stub_ffmpeg
        state["hangs"] = True

        coordinator = LifecycleCoordinator()
        ok = run_ffmpeg(
            "ffmpeg cmd",
            logger=capture_log,
            coordinator=coordinator,
            timeout=0.1,
        )

        assert ok is False
        assert capture_log.has("error", "处理时长超出")
        # 超时终止后子进程已从登记处摘除
        assert coordinator._children == []

    def test_fast_abort_raises(self, stub_ffmpeg, capture_log):
        _calls, _state = stub_ffmpeg
        coordinator = LifecycleCoordinator()
        coordinator.request_abort()

        with pytest.raises(TransferAbortedError):
            run_ffmpeg(
                "ffmpeg cmd",
                logger=capture_log,
                coordinator=coordinator,
            )

        assert coordinator._children == []
        # 中止路径无 end 块：不追加终写状态行
        assert not capture_log.has("info", "【已完成】")

    @pytest.mark.parametrize("returncode", [0, -15, 255])
    def test_abort_beats_returncode_at_final_gate(
        self, stub_ffmpeg, capture_log, monkeypatch, returncode
    ):
        """收尾终审：中止在场时返回码不作数。

        rc=0 复现 POSIX 竞态——子进程被 psutil 终止并回收后，Popen 的
        waitpid 命中 ECHILD，subprocess 会把返回码映射为 0；rc=-15/255
        是未被回收时的真实被杀退出码。两者都不得定性为成功或失败。
        """
        _calls, _state = stub_ffmpeg
        coordinator = LifecycleCoordinator()

        class ReapedPopen:
            def __init__(self):
                self.stderr = io.BytesIO(b"")
                self.stdout = io.BytesIO(b"")
                self.pid = 4321
                self.returncode = returncode

            def poll(self):
                # 中止在主循环轮询间隙落地、子进程被外部终止并回收：
                # 循环顶部的中止检查已错过，只剩收尾处可拦截
                coordinator.request_abort()
                return self.returncode

            def wait(self, timeout=None):
                return self.returncode

        monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kwargs: ReapedPopen())

        with pytest.raises(TransferAbortedError):
            run_ffmpeg("ffmpeg cmd", logger=capture_log, coordinator=coordinator)

        assert not capture_log.has("info", "处理完成")
        assert not capture_log.has("error", "FFmpeg 处理失败")
        assert coordinator._children == []

    def test_popen_oserror_fails_gracefully(self, monkeypatch, capture_log):
        def boom(cmd, **kwargs):
            raise OSError("ffmpeg not found")

        monkeypatch.setattr(subprocess, "Popen", boom)
        coordinator = LifecycleCoordinator()

        ok = run_ffmpeg("ffmpeg cmd", logger=capture_log, coordinator=coordinator)

        assert ok is False
        assert capture_log.has("error", "无法启动 FFmpeg")
