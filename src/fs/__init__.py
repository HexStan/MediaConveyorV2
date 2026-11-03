"""统一文件系统操作包。

本地文件 I/O 原语按职责拆分为六个模块：errors（异常）、abort（中止信号）、
scan（路径查询与目录枚举）、atomic（原子写入）、transfer（传输与删除）、
cleanup（空目录清理）。对外提供一致的日志、错误处理和返回类型。

原则：
- 不依赖 AppContext，不读写状态库，不更新 stats
- 日志通过标准 logging 模块输出，调用方通过配置 handler 控制去向
- 每个函数只做一件事，不混合业务逻辑
"""

from src.fs.abort import TransferAbortedError, abort_requested, set_abort_check
from src.fs.atomic import atomic_writer
from src.fs.cleanup import clean_empty_dirs
from src.fs.errors import FsOpError, PathNotFoundError, PermissionDeniedError
from src.fs.scan import (
    DirEntry,
    StatResult,
    ensure_dir,
    get_dir_size_and_mtime,
    get_stat,
    is_dir,
    list_dir,
    path_exists,
)
from src.fs.transfer import (
    copy_file,
    delete_dir,
    delete_file,
    is_file_locked,
    move_file,
)

__all__ = [
    "DirEntry",
    "FsOpError",
    "PathNotFoundError",
    "PermissionDeniedError",
    "StatResult",
    "TransferAbortedError",
    "abort_requested",
    "atomic_writer",
    "clean_empty_dirs",
    "copy_file",
    "delete_dir",
    "delete_file",
    "ensure_dir",
    "get_dir_size_and_mtime",
    "get_stat",
    "is_dir",
    "is_file_locked",
    "list_dir",
    "move_file",
    "path_exists",
    "set_abort_check",
]
