"""应用运行时路径解析。

统一处理两种运行形态下的路径差异：
- 源码运行（python main.py）：应用根目录 = 仓库根目录；
- PyInstaller 打包运行（onedir）：应用根目录 = 可执行文件所在目录，
  用户可编辑的 config/、logs/、data/ 等均位于该目录下。

打包时内置的 FFmpeg 由 PyInstaller 放入 _internal/ffmpeg/，
ensure_bundled_ffmpeg_on_path() 负责在启动时把该目录前置到 PATH，
使配置模板与探测代码中的裸 ffmpeg / ffprobe 命令解析到内置版本；
源码运行形态不注入，沿用系统 PATH 中的安装。
"""

import os
import sys

import src
from src.platform_info import exe_name, is_windows


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


def bundled_ffmpeg_dir() -> str | None:
    """打包版内置 FFmpeg 目录（含 ffmpeg / ffprobe）；未打包或内置缺失时返回 None。"""
    if not is_frozen():
        return None

    base = getattr(sys, "_MEIPASS", None)
    if not base:
        return None

    path = os.path.join(base, "ffmpeg")
    for name in (exe_name("ffmpeg"), exe_name("ffprobe")):
        if not os.path.isfile(os.path.join(path, name)):
            return None
    return path


def ensure_bundled_ffmpeg_on_path() -> bool:
    """把内置 FFmpeg 目录前置到 PATH；返回是否发生了注入。

    - 仅冻结形态生效（源码运行沿用系统 PATH 中的安装）；
    - 幂等：目录已位于 PATH 首位时不再重复注入；
    - 前置而非注入绝对路径：ffmpeg 命令由 media 层以固定名注入
      （src/core/media/ffmpeg.py 的 FFMPEG_BIN），探测代码的 ffprobe
      亦按裸名调用，二者因此都解析到内置版本。
    """
    ffmpeg_dir = bundled_ffmpeg_dir()
    if ffmpeg_dir is None:
        return False

    _ensure_executable(os.path.join(ffmpeg_dir, exe_name("ffmpeg")))
    _ensure_executable(os.path.join(ffmpeg_dir, exe_name("ffprobe")))

    entries = [entry for entry in os.environ.get("PATH", "").split(os.pathsep) if entry]
    if entries and entries[0] == ffmpeg_dir:
        return False
    os.environ["PATH"] = os.pathsep.join([ffmpeg_dir, *entries])
    return True


def _ensure_executable(path: str) -> None:
    """兜底确保二进制具有可执行权限（防打包/解压过程丢失执行位）。"""
    if is_windows():
        return
    if not os.access(path, os.X_OK):
        try:
            os.chmod(path, 0o755)
        except OSError:
            pass
