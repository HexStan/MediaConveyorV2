"""原子写入原语：同目录临时文件 + fsync + os.replace。"""

import contextlib
import logging
import os
import tempfile
from collections.abc import Callable, Generator
from typing import BinaryIO

from src.fs.scan import ensure_dir

logger = logging.getLogger(__name__)

# 临时文件后缀（公开常量：文件过滤器据此把本工具的瞬态文件排除在
# 待处理名单之外）。采用确定性命名（目标同目录 + 固定后缀），
# 是「再次写入同名文件时清理上次崩溃残留」的前提。
PARTIAL_SUFFIX = ".mc-part"


def _partial_temp_path(dest_path: str) -> str:
    """生成与目标同目录的确定性临时文件路径。

    与目标同目录保证二者位于同一文件系统，最后的 os.replace 才是原子操作。
    """
    dest_dir = os.path.dirname(dest_path) or "."
    filename = os.path.basename(dest_path)
    return os.path.join(dest_dir, f".{filename}{PARTIAL_SUFFIX}")


def _discard_partial(temp_path: str) -> None:
    """尽力删除临时文件（本次写入失败或上次崩溃的残留）。"""
    try:
        os.remove(temp_path)
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.debug(f"清理临时文件失败: {temp_path} — {e}")


def _create_temp_file(dest_path: str) -> str:
    """清理同名残留后独占创建临时文件，返回临时文件路径。

    优先使用确定性名字，残留清理依赖该名字（单实例锁保证同名残留
    只能来自上次崩溃的写入）；名字被占用时回退随机名。
    """
    dest_dir = os.path.dirname(dest_path) or "."
    ensure_dir(dest_dir)

    temp_path = _partial_temp_path(dest_path)
    _discard_partial(temp_path)

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(temp_path, flags, 0o644)
        os.close(fd)
        return temp_path
    except OSError as e:
        logger.debug(f"按确定性名字创建临时文件失败，回退随机名: {temp_path} — {e}")

    fd, fallback_path = tempfile.mkstemp(
        dir=dest_dir,
        prefix=f".{os.path.basename(dest_path)}.",
        suffix=PARTIAL_SUFFIX,
    )
    os.close(fd)
    return fallback_path


@contextlib.contextmanager
def atomic_writer(
    dest_path: str, finalize: Callable[[str], None] | None = None
) -> Generator[BinaryIO]:
    """以原子方式写入 dest_path。

    用法::

        with atomic_writer(dest, finalize=fn) as f:
            f.write(data)

    临时文件创建于目标同目录，正常退出时按
    「flush → finalize（可选）→ fsync → os.replace」收尾，
    任何时刻观察者看到的都是完整的旧文件或完整的新文件；
    异常退出时关闭并删除临时文件，最终路径不受影响。

    注意 finalize 必须在 flush 之后：数据落盘会刷新 mtime，
    copystat/utime 等元数据回写只有在其后执行才能生效。

    Args:
        dest_path: 最终目标路径。
        finalize: 可选回调 finalize(temp_path)，在 fsync 之前执行，
            用于写入元数据（copystat、utime、chmod 等）。
    """
    temp_path = _create_temp_file(dest_path)
    finished = False
    try:
        with open(temp_path, "wb") as f:
            yield f
            f.flush()
            if finalize is not None:
                finalize(temp_path)
            os.fsync(f.fileno())
        os.replace(temp_path, dest_path)
        finished = True
    finally:
        if not finished:
            _discard_partial(temp_path)
