"""终端底部状态栏：日志面板的反色状态行。

渲染依赖 ANSI VT 序列，核心是 DECSTBM 滚动区域：把滚动范围限制在
终端第 1..H-1 行，日志照常逐行滚动；状态栏独占第 H 行（区域之外），
天然常驻最后一行，不与日志争抢输出。零依赖，仅标准序列 + Windows
控制台的 VT 开关。

缩放重绘：轮询检测到终端尺寸变化时整体清屏，按新尺寸重新铺排日志
并贴底（内容源由调用方注入，见 ``ContentProvider``）——终端缩放
不会清除已打印内容，不清屏重画的话，旧状态栏会悬在屏幕中部，放大
产生的空行也永远残留（日志无新内容时没有滚动去推出它们）。

优雅降级：输出流不是真实终端、或 Windows 传统控制台无法启用 VT 时，
``create()`` 返回 None，调用方回退为普通逐行输出。状态栏一旦启用，
渲染异常一律自我停用——它是锦上添花，绝不允许打断日志跟随。
"""

import contextlib
import ctypes
import os
import shutil
import unicodedata
from collections.abc import Callable
from typing import TextIO

from src.platform_info import is_windows

# Windows 控制台模式位：ENABLE_VIRTUAL_TERMINAL_PROCESSING
_ENABLE_VT_PROCESSING = 0x0004
_STD_OUTPUT_HANDLE = -11

# 状态栏右侧的操作提示
_HINT = "Ctrl+C 退出跟随"

# 状态源：每次调用返回 (实例是否运行中, PID)
StatusProvider = Callable[[], tuple[bool, int | None]]

# 重绘内容源：入参为滚动区可用行数，返回按序、将贴底展示的内容行
# （跟随器注入最近输出的日志行，供缩放时全屏重绘）
ContentProvider = Callable[[int], list[str]]


def _enable_windows_vt() -> bool:
    """为当前控制台启用 VT 序列处理（仅 Windows 有此步骤）。

    非 Windows 平台主流终端原生支持，直接返回 True；Windows 传统
    控制台（conhost）默认关闭 VT，需经 SetConsoleMode 打开。返回
    是否可用，不可用时由调用方降级为无状态栏。
    """
    if not is_windows():
        return True
    # pyrefly 按 Linux 语义检查（见 pyproject.toml 的 [tool.pyrefly]），ctypes.windll 是
    # Windows 专属属性，此处运行时已被平台判定守卫
    kernel32 = ctypes.windll.kernel32  # type: ignore[pyrefly:missing-attribute]
    handle = kernel32.GetStdHandle(_STD_OUTPUT_HANDLE)
    mode = ctypes.c_uint32()
    if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        return False  # 句柄不是控制台设备，写 VT 序列只会得到乱码
    if mode.value & _ENABLE_VT_PROCESSING:
        return True
    return bool(kernel32.SetConsoleMode(handle, mode.value | _ENABLE_VT_PROCESSING))


def _display_width(text: str) -> int:
    """文本在终端上的显示宽度：全角/宽字符计 2，其余计 1。"""
    return sum(2 if unicodedata.east_asian_width(char) in ("F", "W") else 1 for char in text)


def _truncate(text: str, width: int) -> str:
    """按显示宽度截断文本：从左取到恰好不超过 width 为止。"""
    chars: list[str] = []
    used = 0
    for char in text:
        used += 2 if unicodedata.east_asian_width(char) in ("F", "W") else 1
        if used > width:
            break
        chars.append(char)
    return "".join(chars)


def _physical_rows(text: str, columns: int) -> int:
    """文本在给定列宽下占用的物理行数：按显示宽度折行上取整。

    空行占 1 行，宽字符计 2 列。制表符展开、零宽字符等极端内容可能
    偏差 1 行，且只会向"多滚动一行"方向偏差（内容仍贴底），可接受。
    """
    if columns <= 0:
        return 1
    width = _display_width(text)
    return max(1, (width + columns - 1) // columns)


class StatusBar:
    """钉在终端最后一行的反色状态栏。

    Args:
        out: 输出流，须为真实终端（``create`` 已保证）。
        provider: 状态源，每次调用返回 ``(运行中, PID)``。
        size_query: 终端尺寸查询，默认 ``shutil.get_terminal_size``（测试可注入）。
        content_provider: 重绘内容源（见模块 docstring）；None 则缩放时
            仅重设滚动区域并重画状态栏，不重绘日志内容。
    """

    def __init__(
        self,
        out: TextIO,
        provider: StatusProvider,
        size_query: Callable[[], os.terminal_size] = shutil.get_terminal_size,
        content_provider: ContentProvider | None = None,
    ) -> None:
        self._out = out
        self._provider = provider
        self._size_query = size_query
        self._content_provider = content_provider
        self._size: tuple[int, int] | None = None  # 上次绘制时的 (列数, 行数)
        self._status: tuple[bool, int | None] | None = None  # 上次绘制时的状态
        self._disabled = False  # 渲染出错后自我停用
        self._closed = False  # close() 已恢复过终端

    @classmethod
    def create(
        cls,
        out: TextIO | None,
        provider: StatusProvider | None,
        vt_opener: Callable[[], bool] = _enable_windows_vt,
        size_query: Callable[[], os.terminal_size] = shutil.get_terminal_size,
        content_provider: ContentProvider | None = None,
    ) -> StatusBar | None:
        """创建状态栏；环境不支持时返回 None，调用方回退普通输出。

        需同时满足：输出流为真实终端、Windows 传统控制台可启用 VT、
        状态源已注入。任一不满足即返回 None，调用方回退普通输出。
        ``content_provider`` 注入后，跟随中的终端缩放触发全屏重绘
        （清屏 + 日志按新尺寸贴底重新铺排）。
        """
        if out is None or provider is None or not getattr(out, "isatty", lambda: False)():
            return None
        if not vt_opener():
            return None
        return cls(out, provider, size_query, content_provider)

    def start(self) -> None:
        """进入跟随前调用：设置滚动区域并完成首次绘制。

        先在当前光标处换行一次（此刻滚动区域尚未设置，为全屏滚动）：
        光标正停在回车后的空行上，这次换行把既有内容（如 shell 命令行）
        整体上推一行，确保随后跳入的滚动区域底行（第 H-1 行）必为空行，
        首次绘制与日志回放不会覆盖任何既有内容。
        """
        try:
            if self._size_query().lines >= 2:
                self._write("\n")
            self._setup(force=True)
        except Exception:  # noqa: BLE001
            self._disable()

    def refresh(self) -> None:
        """跟随循环每轮调用：终端尺寸或实例状态变化时重绘。"""
        if self._disabled:
            return
        try:
            self._setup()
        except Exception:  # noqa: BLE001
            self._disable()

    def close(self) -> None:
        """退出跟随前调用：恢复全屏滚动区域并清除状态行。

        即使因渲染异常已停用，只要绘制过状态行就必须恢复终端；
        从未成功绘制（``start`` 前或环境不支持）则不写任何序列。
        恢复失败只能放弃——跟随即将退出，进程不再使用该终端。
        """
        if self._size is None or self._closed:
            return
        self._closed = True
        self._disabled = True
        with contextlib.suppress(Exception):
            rows = self._size_query().lines
            self._write(f"\x1b[r\x1b[{rows};1H\x1b[0m\x1b[2K")

    # ---- 内部实现 ----------------------------------------------------

    def _disable(self) -> None:
        """自我停用后续轮询的重绘；终端恢复仍由 close() 负责。"""
        self._disabled = True

    def _setup(self, force: bool = False) -> None:
        size = self._size_query()
        columns, rows = size.columns, size.lines
        if rows < 2:
            self._disabled = True  # 至少需要 1 行滚动区 + 1 行状态栏
            return

        status = self._provider()
        size_changed = force or (columns, rows) != self._size
        if not size_changed and status == self._status:
            return

        if size_changed:
            if not force and self._content_provider is not None:
                # 跟随中的缩放：全屏重绘（见 _repaint）。首次 start（force）
                # 不清屏，保持"追加、不覆盖既有内容"的语义
                self._repaint(columns, rows)
            else:
                # 重设滚动区域（日志只在第 1..H-1 行滚动）；DECSTBM 会把光标
                # 归位，随后的状态栏绘制与光标回移会落到确定位置
                self._write(f"\x1b[1;{rows - 1}r")
        self._size = (columns, rows)
        self._status = status

        self._draw(columns, rows, status)
        # 光标放回滚动区域底部行首，后续日志从那里自然滚动输出
        self._write(f"\x1b[{rows - 1};1H")

    def _repaint(self, columns: int, rows: int) -> None:
        """缩放后的全屏重绘：清屏后按新尺寸重新铺排日志并贴底。

        终端缩放不会清除已打印内容：旧状态栏文字悬在屏幕中部，放大
        产生的空行也无法消除，且日志无新内容时永远不会被滚动推出，
        只能整体清屏重画。重绘序列合并为单次写入，避免清屏与重绘
        之间出现闪烁。
        """
        capacity = rows - 1  # 滚动区可用行数
        lines = self._content_provider(capacity) if self._content_provider else []
        # 顶部补空行使内容贴底；占用按物理行（含折行）计。估算偏差只会
        # 引起"多滚动一行"（仍贴底），不会在底部留出空隙
        used = sum(_physical_rows(line, columns) for line in lines)
        pad = max(0, capacity - used)
        sequence = ["\x1b[r\x1b[2J\x1b[H", f"\x1b[1;{capacity}r"]
        if lines:
            sequence.append("\r\n" * pad)
            sequence.append("\r\n".join(lines))
        self._write("".join(sequence))

    def _draw(self, columns: int, rows: int, status: tuple[bool, int | None]) -> None:
        text = self._render(columns, status)
        # 反色期间 \x1b[K 以当前背景（反色后的前景色）填充到行尾，
        # 恰好铺满整行，也避免触碰最右下角字符引发意外折行
        self._write(f"\x1b[{rows};1H\x1b[7m{text}\x1b[K\x1b[0m")

    def _render(self, columns: int, status: tuple[bool, int | None]) -> str:
        running, pid = status
        if not running:
            left = "○ 实例未运行"
        elif pid:
            left = f"● 实例运行中 (PID {pid})"
        else:
            left = "● 实例运行中"
        hint_width = _display_width(_HINT)
        if columns <= hint_width:
            return _truncate(_HINT, columns)
        available = columns - hint_width - 1
        if _display_width(left) > available:
            # 窄终端兜底：截断左侧文本，保证整行（含 1 个分隔空格）不超
            # 列数——状态栏自身折行会在屏上留下第二行残留
            left = _truncate(left, available)
        pad = columns - _display_width(left) - hint_width
        return f"{left}{' ' * pad}{_HINT}"

    def _write(self, text: str) -> None:
        self._out.write(text)
        self._out.flush()
