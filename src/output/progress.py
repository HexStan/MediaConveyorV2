"""FFmpeg 进度的多路输出：日志文件定宽原位覆盖 + 控制台单行覆盖/追加。

run_ffmpeg 把渲染完成的中文进度行交给我分发；行内已含计时器，无新进度
块时也以最新快照高频重调 update()——计时器由此持续走动，落笔节奏由各
sink 依自身间隔决定：

- 日志文件（5 秒）：进度行不追加，而是以定宽字节在文件尾部原位覆盖，
  全程只占一行。定宽保证重写不改变文件长度：``log`` 跟随器按偏移量增量
  读取，文件回缩会触发其从头重放，定宽覆盖下永不回缩。期间其他线程的
  日志行若追加在进度行之后，旧进度行让位保留，覆盖区间重锚到新的文件
  尾；按天轮转后自动在新日志文件锚定。
- 控制台 TTY（1 秒）：``\\r`` 单行覆盖，按显示宽度补空格防残留，不依赖
  VT 序列；结束时覆盖一次后换行归还终端行。期间其他线程的日志经
  ConsoleLogHandler 先清行再输出，进度行由下一轮刷新重绘。
- 控制台非 TTY（5 秒）：整行追加；结束时追加一次。

``progress=end`` 的终写由 finish() 承担，绕过节流；异常结束（命令失败/
超时/fast 中止，不会有 end 块）调用 close()：不写进度内容，仅归还终端
行与清行登记，最后一行进度原样保留。
"""

import logging
import os
import time
from collections.abc import Callable

from src.output.logger import ConsoleLogHandler, DailyRotatingFileHandler
from src.output.statusbar import display_width

# 各输出的落笔间隔（秒）
_FILE_INTERVAL = 5.0
_TTY_INTERVAL = 1.0
_PLAIN_INTERVAL = 5.0

# 日志文件进度行的定宽（字节）：宽度内空格填充、超宽整字符安全截断。
# 实际渲染约 250 字节，512 留出帧号/体积增长的余量。
_LINE_WIDTH_BYTES = 512


def _fit_fixed_width(line: str, width: int) -> bytes:
    """渲染行适配定宽字节：超宽按整字符安全截断，不足以空格填充。"""
    data = line.encode("utf-8")
    if len(data) > width:
        data = data[:width].decode("utf-8", errors="ignore").encode("utf-8")
    return data.ljust(width)


class ProgressReporter:
    """FFmpeg 进度行的多路输出分发器。

    Args:
        file_sink: 日志文件 sink，None 表示无日志文件可写。
        console_sink: 控制台 sink，None 表示无控制台（分离式实例）。
    """

    def __init__(
        self,
        file_sink: _LogFileOverwriter | None,
        console_sink: _ConsolePrinter | None,
    ) -> None:
        self._file = file_sink
        self._console = console_sink

    def update(self, line: str) -> None:
        """分发一次进度行；各 sink 依自身间隔决定是否落笔。"""
        if self._file is not None:
            self._file.update(line)
        if self._console is not None:
            self._console.update(line)

    def finish(self, line: str) -> None:
        """progress=end 的终写：绕过节流，各 sink 最后落笔一次。"""
        if self._file is not None:
            self._file.finish(line)
        if self._console is not None:
            self._console.finish(line)

    def close(self) -> None:
        """异常结束的收尾：不写进度内容，仅归还终端行与清行登记。"""
        if self._console is not None:
            self._console.close()


def reporter_from_handlers(handlers: list[logging.Handler]) -> ProgressReporter | None:
    """从 logger 的 handler 列表装配分发器；无任何可用输出时返回 None。"""
    file_handler = next(
        (handler for handler in handlers if isinstance(handler, DailyRotatingFileHandler)),
        None,
    )
    console_handler = next(
        (handler for handler in handlers if isinstance(handler, ConsoleLogHandler)),
        None,
    )
    if file_handler is None and console_handler is None:
        return None
    return ProgressReporter(
        file_sink=_LogFileOverwriter(file_handler) if file_handler else None,
        console_sink=_ConsolePrinter(console_handler) if console_handler else None,
    )


class _LogFileOverwriter:
    """日志文件 sink：进度行定宽原位覆盖（5 秒节流）。

    覆盖区间（region）锚定在日志文件 EOF；重写期间持有文件 handler 的
    锁，与 logging 的 emit 互斥——他线日志行不会落入区间内被截毁。
    """

    def __init__(
        self,
        handler: DailyRotatingFileHandler,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._handler = handler
        self._clock = clock
        self._interval = _FILE_INTERVAL
        self._last = float("-inf")
        self._path: str | None = None  # 当前锚定的日志文件
        self._offset = 0  # region 起始字节偏移

    def update(self, line: str) -> None:
        now = self._clock()
        if now - self._last < self._interval:
            return
        if self._render(line):
            self._last = now

    def finish(self, line: str) -> None:
        """终写：绕过节流，以 progress=end 时刻的进度最后覆盖一次。"""
        self._render(line)

    # ---- 内部实现 ----------------------------------------------------

    def _render(self, line: str) -> bool:
        data = _fit_fixed_width(line, _LINE_WIDTH_BYTES) + b"\n"
        handler = self._handler
        # 与 logging 的 emit 互斥：他线日志行不会落入覆盖区间被截毁
        handler.acquire()
        try:
            path = handler.baseFilename
            if path != self._path:
                # 首写或按天轮转：在新文件 EOF 锚定（旧文件的进度行原样保留）
                self._path = path
                self._offset = os.path.getsize(path)
            else:
                size = os.path.getsize(path)
                if size != self._offset + len(data):
                    # 区间之后出现了其他内容（他线日志追加，或文件被外部
                    # 截断）：旧进度行让位保留，重锚到当前 EOF
                    self._offset = size
            self._write(path, data)
        except OSError:
            # 轮转间隙新文件尚未创建等：复位锚定，下个间隔重试
            self._path = None
            return False
        else:
            return True
        finally:
            handler.release()

    def _write(self, path: str, data: bytes) -> None:
        # 等长重写不改变文件长度；锚定/重锚时写入点即 EOF，truncate 同为无操作
        with open(path, "r+b") as f:
            f.seek(self._offset)
            f.write(data)
            f.truncate()


class _ConsolePrinter:
    """控制台 sink：TTY 单行覆盖（1 秒节流），非 TTY 整行追加（5 秒节流）。

    TTY 覆盖行按显示宽度补空格防残留，不依赖 VT 序列；首次落笔后把自身
    登记到 ConsoleLogHandler（progress_writer），其他线程的日志 emit 前
    会回调 clear_line 清开进度行。写入失败（流关闭等）后自我停用。
    """

    def __init__(
        self,
        handler: ConsoleLogHandler,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._handler = handler
        self._stream = handler.stream
        self._clock = clock
        self._tty = self._detect_tty()
        self._interval = _TTY_INTERVAL if self._tty else _PLAIN_INTERVAL
        self._last = float("-inf")
        self._width = 0  # 当前 TTY 覆盖行的显示宽度（0 表示屏上无进度行）
        self._registered = False
        self._dead = False

    def update(self, line: str) -> None:
        now = self._clock()
        if now - self._last < self._interval:
            return
        if self._render(line):
            self._last = now

    def finish(self, line: str) -> None:
        """终写：绕过节流。TTY 覆盖一次后换行归还终端行，随后注销登记。"""
        self._render(line)
        if self._tty:
            self._end_line()
        self._unregister()

    def close(self) -> None:
        """异常结束收尾：不覆盖内容，TTY 上仅换行归还终端行并注销登记。"""
        if self._tty and self._width:
            self._write_raw("\n")
        self._width = 0
        self._unregister()

    def clear_line(self) -> None:
        """清开屏上的进度行（ConsoleLogHandler.emit 在 handler 锁内回调）。"""
        if self._width:
            self._write_raw("\r" + " " * self._width + "\r")

    # ---- 内部实现 ----------------------------------------------------

    def _detect_tty(self) -> bool:
        isatty = getattr(self._stream, "isatty", None)
        return bool(isatty and isatty())

    def _render(self, line: str) -> bool:
        if self._dead:
            return False
        if self._tty:
            width = display_width(line)
            self._width = max(self._width, width)
            if not self._write_raw("\r" + line + " " * (self._width - width)):
                return False
            self._register()
            return True
        return self._write_raw(line + "\n")

    def _write_raw(self, text: str) -> bool:
        self._handler.acquire()
        try:
            self._stream.write(text)
            self._stream.flush()
        except OSError, ValueError:
            self._dead = True
            self._unregister()
            return False
        else:
            return True
        finally:
            self._handler.release()

    def _end_line(self) -> None:
        if self._width:
            self._write_raw("\n")
        self._width = 0

    def _register(self) -> None:
        if not self._registered:
            self._handler.progress_writer = self
            self._registered = True

    def _unregister(self) -> None:
        if self._registered:
            self._handler.progress_writer = None
            self._registered = False
