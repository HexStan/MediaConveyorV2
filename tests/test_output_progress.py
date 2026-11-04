"""单元测试：FFmpeg 进度的多路输出设施（src/output/progress.py）。

落笔节奏（文件 5 秒 / TTY 1 秒 / 非 TTY 5 秒）与覆盖形态是外部不可见的
契约：真实时钟下无法观察，只能注入时钟白盒兜底（tests/README.md 原则
二、原则四）。日志文件的定宽原位覆盖以真实文件断言——文件长度任何时刻
不回缩、覆盖区间等长重写，是 ``log`` 跟随器零重放的前提；控制台的 TTY
覆盖、清行协调与异常收尾以记录型流断言。
"""

import logging
import os

from src.output.logger import ConsoleLogHandler, DailyRotatingFileHandler, setup_logger
from src.output.progress import (
    _LINE_WIDTH_BYTES,
    _ConsolePrinter,
    _fit_fixed_width,
    _LogFileOverwriter,
    reporter_from_handlers,
)


class _FakeClock:
    """确定性时钟：按预设序列返回，耗尽后停在末值。"""

    def __init__(self, *moments: float):
        self._moments = list(moments)
        self._index = 0

    def __call__(self) -> float:
        value = self._moments[min(self._index, len(self._moments) - 1)]
        self._index += 1
        return value


class _FakeStream:
    """可配置 isatty 的记录型流：按序记录全部写入内容。"""

    def __init__(self, tty: bool):
        self.tty = tty
        self.chunks: list[str] = []

    def isatty(self) -> bool:
        return self.tty

    def write(self, text: str) -> int:
        self.chunks.append(text)
        return len(text)

    def flush(self) -> None:
        pass


class _BrokenStream:
    """写入即失败的流：验证 sink 的自我停用。"""

    def isatty(self) -> bool:
        return True

    def write(self, text: str) -> int:
        raise OSError("stream closed")

    def flush(self) -> None:
        pass


def _read(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _make_handler(tmp_path) -> DailyRotatingFileHandler:
    """真实日志 handler：写入一行前置日志，保证锚定前文件非空。"""
    handler = DailyRotatingFileHandler(str(tmp_path), 0)
    handler.emit(logging.LogRecord("t", logging.INFO, __file__, 1, "前置日志", None, None))
    return handler


class TestFitFixedWidth:
    def test_short_line_padded(self):
        data = _fit_fixed_width("【进行中】", _LINE_WIDTH_BYTES)

        assert data == "【进行中】".encode().ljust(_LINE_WIDTH_BYTES)

    def test_overlong_line_truncated_at_char_boundary(self):
        line = "帧" * 400  # 1200 字节，远超定宽

        data = _fit_fixed_width(line, _LINE_WIDTH_BYTES)

        assert len(data) == _LINE_WIDTH_BYTES
        # 截断不产生坏字节：整行可无损解码
        assert data.decode("utf-8").encode("utf-8") == data
        assert data.decode("utf-8") == "帧" * 170 + " " * 2


class TestLogFileOverwriter:
    def test_region_overwritten_in_place_without_growth(self, tmp_path):
        handler = _make_handler(tmp_path)
        sink = _LogFileOverwriter(handler, clock=_FakeClock(0.0, 10.0))
        log_file = handler.baseFilename

        sink.update("第一帧")
        size_after_first = os.path.getsize(log_file)
        assert _read(log_file)[-513:] == _fit_fixed_width("第一帧", _LINE_WIDTH_BYTES) + b"\n"

        sink.update("更短的行")
        # 等长原位重写：文件长度不变，log 跟随器不会观察到回缩
        assert os.path.getsize(log_file) == size_after_first
        region = _read(log_file)[-513:]
        assert "更短的行".encode() in region
        assert "第一帧".encode() not in region

    def test_throttled_between_intervals(self, tmp_path):
        handler = _make_handler(tmp_path)
        sink = _LogFileOverwriter(handler, clock=_FakeClock(0.0, 1.0, 6.0))
        log_file = handler.baseFilename

        sink.update("A")
        after_first = _read(log_file)
        sink.update("B")  # 1 秒 < 5 秒节流：不落笔
        assert _read(log_file) == after_first
        sink.update("C")  # 6 秒 ≥ 5 秒：落笔
        assert b"C" in _read(log_file)

    def test_finish_bypasses_throttle(self, tmp_path):
        handler = _make_handler(tmp_path)
        sink = _LogFileOverwriter(handler, clock=_FakeClock(0.0, 0.5))
        log_file = handler.baseFilename

        sink.update("进行中")
        sink.finish("已完成")  # 距上次落笔不足 5 秒也必须覆盖

        assert "已完成".encode() in _read(log_file)

    def test_foreign_log_append_relocates_region(self, tmp_path):
        """他线日志追加在进度行之后：旧行让位保留，覆盖区间重锚到新 EOF。"""
        handler = _make_handler(tmp_path)
        sink = _LogFileOverwriter(handler, clock=_FakeClock(0.0, 10.0))
        log_file = handler.baseFilename

        sink.update("进度A")
        handler.emit(logging.LogRecord("t", logging.INFO, __file__, 1, "中途日志", None, None))
        sink.update("进度B")

        content = _read(log_file)
        assert "进度A".encode() in content
        assert "中途日志".encode() in content
        assert content.endswith(_fit_fixed_width("进度B", _LINE_WIDTH_BYTES) + b"\n")

    def test_log_file_rotation_reanchors(self, tmp_path):
        """日志文件切换（按天轮转）：旧文件的进度行保留，新文件重新锚定。"""
        handler = _make_handler(tmp_path)
        sink = _LogFileOverwriter(handler, clock=_FakeClock(0.0, 10.0))
        old_path = handler.baseFilename

        sink.update("旧日进度")
        new_path = os.path.join(str(tmp_path), "mediaconveyor.2000-01-02.log")
        with open(new_path, "wb") as f:
            f.write("新文件日志\n".encode())
        handler.baseFilename = new_path  # 模拟轮转后的当前文件切换
        sink.update("新日进度")

        assert "旧日进度".encode() in _read(old_path)
        assert "新日进度".encode() in _read(new_path)

    def test_missing_file_recovers_on_next_interval(self, tmp_path):
        """锚定目标不存在（轮转间隙）：静默失败，下个间隔重试成功。"""
        handler = _make_handler(tmp_path)
        sink = _LogFileOverwriter(handler, clock=_FakeClock(0.0, 1.0, 6.0))
        # 模拟轮转间隙：当前文件名已切换，新文件尚未由 handler 创建
        missing = os.path.join(str(tmp_path), "mediaconveyor.2000-01-02.log")
        handler.baseFilename = missing

        sink.update("A")  # 文件不存在：不落笔、不抛错
        with open(missing, "wb"):
            pass
        sink.update("B")  # 文件就绪：重锚落笔

        assert b"B" in _read(missing)


class TestConsolePrinter:
    def test_tty_overwrites_single_line_each_second(self):
        stream = _FakeStream(tty=True)
        printer = _ConsolePrinter(ConsoleLogHandler(stream), clock=_FakeClock(0.0, 0.4, 1.0, 1.4))

        printer.update("【进行中】 A")
        printer.update("【进行中】 B")  # 0.4 秒 < 1 秒：跳过
        printer.update("【进行中】 C")  # 1.0 秒：覆盖
        printer.update("【进行中】 D")  # 1.4 秒 < 1 秒：跳过

        assert stream.chunks == ["\r【进行中】 A", "\r【进行中】 C"]

    def test_tty_pads_to_prevent_residue(self):
        """短行覆盖长行时按显示宽度补空格，屏上不残留旧字符。"""
        stream = _FakeStream(tty=True)
        printer = _ConsolePrinter(ConsoleLogHandler(stream), clock=_FakeClock(0.0, 1.0))

        printer.update("AAAA")
        printer.update("BB")

        assert stream.chunks == ["\rAAAA", "\rBB  "]

    def test_tty_finish_overwrites_then_hands_back_line(self):
        stream = _FakeStream(tty=True)
        handler = ConsoleLogHandler(stream)
        printer = _ConsolePrinter(handler, clock=_FakeClock(0.0, 5.0))

        printer.update("P")
        printer.finish("END")

        assert stream.chunks == ["\rP", "\rEND", "\n"]
        assert handler.progress_writer is None

    def test_tty_close_hands_back_line_without_overwrite(self):
        """异常结束：不覆盖内容，仅换行归还终端行并注销清行登记。"""
        stream = _FakeStream(tty=True)
        handler = ConsoleLogHandler(stream)
        printer = _ConsolePrinter(handler, clock=_FakeClock(0.0, 1.0))

        printer.update("P")
        printer.close()

        assert stream.chunks == ["\rP", "\n"]
        assert handler.progress_writer is None

    def test_log_emission_clears_active_progress_line(self):
        """日志 emit 前清开进度行（协调契约），进度行随后重绘。"""
        stream = _FakeStream(tty=True)
        handler = ConsoleLogHandler(stream)
        printer = _ConsolePrinter(handler, clock=_FakeClock(0.0, 1.0))

        printer.update("进度行内容")
        handler.handle(logging.LogRecord("t", logging.INFO, __file__, 1, "普通日志", None, None))
        printer.update("进度行内容")

        width = len("进度行内容") * 2  # 全角字符显示宽度
        assert stream.chunks == [
            "\r进度行内容",
            "\r" + " " * width + "\r",
            "普通日志\n",
            "\r进度行内容",
        ]

    def test_non_tty_appends_at_interval(self):
        stream = _FakeStream(tty=False)
        printer = _ConsolePrinter(ConsoleLogHandler(stream), clock=_FakeClock(0.0, 4.0, 5.0))

        printer.update("A")
        printer.update("B")  # 4 秒 < 5 秒：跳过
        printer.update("C")  # 5 秒：追加

        assert stream.chunks == ["A\n", "C\n"]

    def test_non_tty_finish_appends_once(self):
        stream = _FakeStream(tty=False)
        printer = _ConsolePrinter(ConsoleLogHandler(stream), clock=_FakeClock(0.0, 1.0))

        printer.update("A")
        printer.finish("终态")

        assert stream.chunks == ["A\n", "终态\n"]

    def test_write_failure_disables_sink(self):
        handler = ConsoleLogHandler(_BrokenStream())
        printer = _ConsolePrinter(handler, clock=_FakeClock(0.0, 1.0))

        printer.update("A")  # 写入失败：自我停用
        printer.update("B")  # 停用后不再尝试

        assert handler.progress_writer is None


class TestReporterFromHandlers:
    def test_assembles_file_and_console_sinks(self, tmp_path, monkeypatch):
        stream = _FakeStream(tty=False)
        monkeypatch.setattr("sys.stderr", stream)
        logger = setup_logger(str(tmp_path), console=True)

        reporter = logger.progress_reporter()
        assert reporter is not None
        reporter.update("进度")

        (log_file,) = [os.path.join(str(tmp_path), name) for name in os.listdir(str(tmp_path))]
        assert _read(log_file) == _fit_fixed_width("进度", _LINE_WIDTH_BYTES) + b"\n"
        assert stream.chunks == ["进度\n"]

    def test_detached_instance_has_file_sink_only(self, tmp_path, monkeypatch):
        stream = _FakeStream(tty=False)
        monkeypatch.setattr("sys.stderr", stream)
        logger = setup_logger(str(tmp_path), console=False)

        reporter = logger.progress_reporter()
        assert reporter is not None
        reporter.update("进度")

        (log_file,) = [os.path.join(str(tmp_path), name) for name in os.listdir(str(tmp_path))]
        assert "进度".encode() in _read(log_file)
        assert stream.chunks == []  # 分离式实例不写控制台

    def test_no_usable_handlers_returns_none(self):
        assert reporter_from_handlers([]) is None
