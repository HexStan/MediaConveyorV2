"""单元测试：FFmpeg 子进程托管（节流与 end 终写时序、超时与 fast 中止）。

Popen 是进程边界允许打桩的接缝（tests/README.md 原则三，桩见 conftest
的 stub_ffmpeg）：FakePopen 模拟 ffmpeg 的输出流（stderr 错误输出 +
stdout progress 块）与退出码，钉死进度行节流与 end 终写时序、超时
终止与 fast 中止的契约。FFmpeg 语义本身不测。
"""

import io
import re
import subprocess

import pytest

from src.app.lifecycle import LifecycleCoordinator
from src.core.media.ffmpeg.execute import run_ffmpeg
from src.fs import TransferAbortedError


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


def _banner_output() -> bytes:
    """ffmpeg 启动横幅的 stderr 形状（Input 头 + Duration 行）。"""
    return (
        b"Input #0, mov,mp4,m4a,3gp,3gpp,mj2, from 'input.mp4':\n"
        b"  Duration: 00:00:08.40, start: 0.000000, bitrate: 8543 kb/s\n"
    )


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
        finished = [m for lv, m in capture_log.messages if lv == "info" and "已完成" in m]
        assert len(finished) == 1

    def test_end_record_carries_zero_eta(self, stub_ffmpeg, capture_log):
        """终写行的 ETA 恒为 00:00（总时长是否在场不影响）。"""
        _calls, state = stub_ffmpeg
        state["progress"] = _continue_block() + _end_block()

        ok = run_ffmpeg("ffmpeg cmd", logger=capture_log, coordinator=LifecycleCoordinator())

        assert ok is True
        finished = next(m for lv, m in capture_log.messages if lv == "info" and "已完成" in m)
        assert "持续时间：00:00 / 00:00" in finished

    def test_stderr_stats_lines_are_plain_error_output(self, stub_ffmpeg, capture_log):
        """-nostats 后 stderr 不再有进度语义：time=/speed= 行只是普通输出行。"""
        _calls, state = stub_ffmpeg
        state["output"] = b"time=00:00:01.00 speed=2.0x\r\n"

        run_ffmpeg("ffmpeg cmd", logger=capture_log, coordinator=LifecycleCoordinator())

        assert not capture_log.has("info", "FFmpeg 进度")
        assert not capture_log.has("info", "进行中")
        assert not capture_log.has("info", "已完成")

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
        progress_logs = [m for lv, m in capture_log.messages if lv == "info" and "进行中" in m]
        assert len(progress_logs) == 1
        assert not capture_log.has("info", "已完成")

    def test_progress_lines_log_every_poll_when_throttle_disabled(
        self, stub_ffmpeg, capture_log, monkeypatch
    ):
        """节流间隔归零（测试替身）：无新块也以最新快照落笔，计时器持续走动。"""
        monkeypatch.setattr("src.core.media.ffmpeg.execute._PROGRESS_LOG_INTERVAL", 0)
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
        progress_logs = [m for lv, m in capture_log.messages if lv == "info" and "进行中" in m]
        assert len(progress_logs) >= 2

    def test_banner_duration_feeds_eta(self, stub_ffmpeg, capture_log, monkeypatch):
        """stderr 启动横幅的 Duration 行被嗅探为总时长，持续时间字段估算 ETA。"""
        monkeypatch.setattr("src.core.media.ffmpeg.execute._PROGRESS_LOG_INTERVAL", 0)
        _calls, state = stub_ffmpeg
        state["hangs"] = True
        state["output"] = _banner_output()
        state["progress"] = _continue_block()

        ok = run_ffmpeg(
            "ffmpeg cmd",
            logger=capture_log,
            coordinator=LifecycleCoordinator(),
            timeout=0.3,
        )

        assert ok is False
        progress_logs = [m for lv, m in capture_log.messages if lv == "info" and "进行中" in m]
        assert progress_logs
        # 首拍可能先于横幅被消费而显示占位符，其后各拍必已解析到总时长
        assert any(re.search(r"持续时间：\d{2,}:\d{2} / \d{2,}:\d{2}", m) for m in progress_logs)
        assert any(re.search(r"输出时间戳：\d{2,}:\d{2} / \d{2,}:\d{2}", m) for m in progress_logs)

    def test_failure_appends_no_end_record(self, stub_ffmpeg, capture_log):
        """命令失败（无 end 块）：不追加终写状态行，最后一行进度原样保留。"""
        _calls, state = stub_ffmpeg
        state["returncode"] = 1
        state["progress"] = _continue_block()

        ok = run_ffmpeg("ffmpeg cmd", logger=capture_log, coordinator=LifecycleCoordinator())

        assert ok is False
        assert capture_log.has("info", "进行中")
        assert not capture_log.has("info", "已完成")

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

    def test_banner_lines_stay_in_error_tail(self, stub_ffmpeg, capture_log):
        """横幅嗅探是被动的：Duration 行仍原样保留在错误尾部输出中。"""
        _calls, state = stub_ffmpeg
        state["returncode"] = 1
        state["output"] = _banner_output() + b"Invalid data found when processing input\n"

        ok = run_ffmpeg("ffmpeg cmd", logger=capture_log, coordinator=LifecycleCoordinator())

        assert ok is False
        error_msg = next(m for lv, m in capture_log.messages if lv == "error")
        assert "Duration: 00:00:08.40" in error_msg

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
        assert not capture_log.has("info", "已完成")

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
