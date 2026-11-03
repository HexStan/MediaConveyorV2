"""应用运行时路径解析。

统一处理两种运行形态下的路径差异：
- 源码运行（python main.py）：应用根目录 = 仓库根目录；
- PyInstaller 打包运行（onedir）：应用根目录 = 可执行文件所在目录，
  用户可编辑的 config/、logs/、data/ 等均位于该目录下。
"""

import os
import sys

import src


def is_frozen() -> bool:
    """是否运行于 PyInstaller 打包环境中。"""
    return getattr(sys, "frozen", False)


def app_base_dir() -> str:
    """应用根目录：打包后为可执行文件所在目录，源码运行为仓库根目录。

    源码形态锚定 ``src`` 包的位置（仓库根 = src 包的父目录），
    而非本模块自身的 ``__file__`` 层级——后者会随模块在包内的
    位置漂移（参见包层级重组时 app_paths.py -> app/paths.py 的教训）。
    """
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    src_dir = os.path.dirname(os.path.abspath(src.__file__))
    return os.path.dirname(src_dir)


def config_path() -> str:
    """配置文件路径：config/config.toml。"""
    return os.path.join(app_base_dir(), "config", "config.toml")


def _resolve_under_base_dir(path: str) -> str:
    """相对路径以应用根目录为基准，与启动时的工作目录无关。"""
    if path and not os.path.isabs(path):
        return os.path.normpath(os.path.join(app_base_dir(), path))
    return path


def resolve_log_dir(log_dir: str) -> str:
    """解析日志目录。"""
    return _resolve_under_base_dir(log_dir)


def resolve_data_dir(data_dir: str) -> str:
    """解析数据目录（SQLite 状态库所在）。"""
    return _resolve_under_base_dir(data_dir)
