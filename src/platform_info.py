"""共享平台探测：运行平台的唯一判定入口。

全仓库的平台判定统一收口于此，禁止散落 sys.platform / os.name /
platform.system() 的裸比较。本模块是仅依赖标准库的叶子模块，
所有包可导入（分层约定见 AGENTS.md）。

设计约束：
- 判定原语统一为 ``sys.platform``：与 os.name 在 CPython 支持的
  全部平台（含 cygwin 边界）上判定等价，且是测试打桩的原语
  （monkeypatch.setattr(sys, "platform", ...)）；
- 因此一律调用时读取，绝不缓存（不用 lru_cache / 模块级常量），
  否则测试桩失效。
"""

import sys
from enum import Enum


class Platform(Enum):
    """运行平台三分类。

    POSIX 涵盖 Linux 及其他非 macOS 的 POSIX 系统（freebsd 等），
    业务上按 Linux 族处理。
    """

    WINDOWS = "windows"  # sys.platform == "win32"
    MACOS = "macos"  # sys.platform == "darwin"
    POSIX = "posix"  # 其余


def current() -> Platform:
    """探测当前运行平台。每次调用实时读取，不缓存。"""
    if sys.platform == "win32":
        return Platform.WINDOWS
    if sys.platform == "darwin":
        return Platform.MACOS
    return Platform.POSIX


def is_windows() -> bool:
    """当前平台是否为 Windows。"""
    return current() is Platform.WINDOWS


def is_macos() -> bool:
    """当前平台是否为 macOS。"""
    return current() is Platform.MACOS


def is_posix() -> bool:
    """当前平台是否为 POSIX 系统（Linux / macOS / freebsd 等）。"""
    return current() is not Platform.WINDOWS


def exe_name(base: str) -> str:
    """按平台补全可执行文件名后缀：Windows 下追加 .exe，其余原样返回。"""
    return f"{base}.exe" if is_windows() else base
