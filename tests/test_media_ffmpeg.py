"""单元测试：FFmpeg 命令构建与进程托管执行。

Popen 是进程边界允许打桩的接缝（tests/README.md 原则三，桩见 conftest
的 stub_ffmpeg）：FakePopen 模拟 ffmpeg 的输出流与退出码，钉死命令模板
填充、进度节流、超时终止与 fast 中止的契约。FFmpeg 语义本身不测。
"""

import subprocess

import pytest

from src.app.lifecycle import LifecycleCoordinator
from src.core.media.ffmpeg import TemplateFormatError, build_command, run_ffmpeg
from src.fs import TransferAbortedError


class TestBuildCommand:
    def test_fills_placeholders(self):
        cmd = build_command('ffmpeg -i "{input}" "{output}.mp4"', "/src/a b.mp4", "/dest/a b")

        assert cmd == 'ffmpeg -i "/src/a b.mp4" "/dest/a b.mp4"'

    def test_multiline_merged_to_single_line(self):
        cmd = build_command('ffmpeg -y \\\n  -i "{input}" \\\n  "{output}.mp4"', "/in", "/out")

        assert "\n" not in cmd
        assert "\r" not in cmd
        assert '-i "/in"' in cmd
        assert '"/out.mp4"' in cmd

    def test_unknown_placeholder_raises(self):
        with pytest.raises(TemplateFormatError, match="占位符"):
            build_command("ffmpeg {oops}", "/in", "/out")

    def test_empty_braces_raise(self):
        with pytest.raises(TemplateFormatError):
            build_command("ffmpeg {}", "/in", "/out")


class TestRunFFmpeg:
    def test_success_logs_report(self, stub_ffmpeg, capture_log):
        calls, state = stub_ffmpeg
        state["output"] = b"frame= 1\r\ntime=00:00:01.00 speed=2.0x\r\n"

        coordinator = LifecycleCoordinator()
        ok = run_ffmpeg("ffmpeg cmd", rel_path="a.mp4", logger=capture_log, coordinator=coordinator)

        assert ok is True
        assert calls == ["ffmpeg cmd"]
        assert capture_log.has("info", "FFmpeg 处理完成")
        assert capture_log.has("info", "FFmpeg 运行报告")

    def test_failure_logs_error_tail(self, stub_ffmpeg, capture_log):
        _calls, state = stub_ffmpeg
        state["returncode"] = 1
        state["output"] = b"\n".join([b"line %d" % i for i in range(30)]) + b"\n"

        coordinator = LifecycleCoordinator()
        ok = run_ffmpeg("ffmpeg cmd", rel_path="a.mp4", logger=capture_log, coordinator=coordinator)

        assert ok is False
        # 只保留末尾 20 行
        error_msg = next(m for lv, m in capture_log.messages if lv == "error")
        assert "line 29" in error_msg
        assert "line 5" not in error_msg

    def test_progress_lines_throttled(self, stub_ffmpeg, capture_log):
        _calls, state = stub_ffmpeg
        # 两条进度行 + 一条非进度行：5 秒节流下只应有首条进度行入日志
        state["output"] = b"time=00:00:01.00 speed=1.0x\r\ntime=00:00:02.00 speed=1.0x\r\nother\r\n"

        coordinator = LifecycleCoordinator()
        run_ffmpeg("ffmpeg cmd", rel_path="a.mp4", logger=capture_log, coordinator=coordinator)

        progress_logs = [m for lv, m in capture_log.messages if lv == "info" and "FFmpeg 进度" in m]
        assert len(progress_logs) == 1

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
            rel_path="a.mp4",
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
            rel_path="a.mp4",
            logger=capture_log,
            coordinator=coordinator,
            timeout=0.1,
        )

        assert ok is False
        assert capture_log.has("error", "处理超时")
        # 超时终止后子进程已从登记处摘除
        assert coordinator._children == []

    def test_fast_abort_raises(self, stub_ffmpeg, capture_log):
        _calls, _state = stub_ffmpeg
        coordinator = LifecycleCoordinator()
        coordinator.request_abort()

        with pytest.raises(TransferAbortedError):
            run_ffmpeg("ffmpeg cmd", rel_path="a.mp4", logger=capture_log, coordinator=coordinator)

        assert coordinator._children == []

    def test_popen_oserror_fails_gracefully(self, monkeypatch, capture_log):
        def boom(cmd, **kwargs):
            raise OSError("ffmpeg not found")

        monkeypatch.setattr(subprocess, "Popen", boom)
        coordinator = LifecycleCoordinator()

        ok = run_ffmpeg("ffmpeg cmd", rel_path="a.mp4", logger=capture_log, coordinator=coordinator)

        assert ok is False
        assert capture_log.has("error", "无法启动 FFmpeg")
