"""文件处理状态库（SQLite）：防重复处理、失败计数与 fallback 切换的依据。

每条记录以 (task_key, filepath) 为主键：task_key 由任务的 (source_dir,
dest_dir) 归一化而来——多个任务监听重叠目录时状态互不串扰，记录对配置
重排序与任务增删稳定。filepath 统一 normcase，Windows 大小写不同的
路径指向同一条记录。

记录的语义——记录的生命周期即义务的生命周期，只在义务存续期间存在：
- 失败计数是重试义务：处理失败后必须重试，failure_count 达到任务级
  failure_count 后转为永久跳过；ffmpeg_failure_count 专计主命令失败，
  达到 fallback_count 后切换备用命令；成功一次即双计数清零；
- 成功记录（success_time 非 NULL，附带成功时的源文件 size/mtime）仅当
  源文件仍留在源树时存在：或承载待删除义务（remove_source 模式，到期
  时刻 = success_time + source_expired_minutes，删除成功即清记录），或
  承载防重复义务（源文件保留在原地；内容变化——size 或 mtime 不再吻合
  ——即失效，重新处理）。源文件离开源树（删除 / 备份 / 直传移走）后
  不留记录。
"""

import contextlib
import os
import sqlite3
from dataclasses import dataclass

# task_key 的组成分隔符：normcase 后的绝对路径不含该字符
_KEY_SEPARATOR = "\x00"


@dataclass(frozen=True)
class SuccessState:
    """已成功处理记录的快照（防重复判定与待删除义务的依据）。"""

    success_time: float
    source_size: int
    source_mtime: float


def task_key(source_dir: str, dest_dir: str) -> str:
    """由任务的两个目录构造状态隔离键（归一化，配置重排稳定）。"""
    source = os.path.normcase(os.path.abspath(source_dir))
    dest = os.path.normcase(os.path.abspath(dest_dir))
    return source + _KEY_SEPARATOR + dest


def file_key(filepath: str) -> str:
    """源文件路径的记录键（normcase，消除 Windows 大小写歧义）。"""
    return os.path.normcase(os.path.abspath(filepath))


class StateManager:
    """SQLite 状态库的读写入口。连接生命周期与运行实例一致。

    连接允许跨线程使用（check_same_thread=False）：生产装配中状态库的
    全部读写都发生在调度循环线程，此开关仅为测试中"主线程装配、子线程
    驱动调度"的形态兜底；写操作以每语句提交收口，无长事务竞争。
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS file_state ("
            "    task_key              TEXT NOT NULL,"
            "    filepath              TEXT NOT NULL,"
            "    success_time          REAL,"
            "    failure_count         INTEGER NOT NULL DEFAULT 0,"
            "    ffmpeg_failure_count  INTEGER NOT NULL DEFAULT 0,"
            "    source_size           INTEGER,"
            "    source_mtime          REAL,"
            "    PRIMARY KEY (task_key, filepath)"
            ")"
        )
        self._conn.commit()

    # ---- 查询 -----------------------------------------------------------

    def get_failure_count(self, key: str, filepath: str) -> int:
        row = self._conn.execute(
            "SELECT failure_count FROM file_state WHERE task_key = ? AND filepath = ?",
            (key, filepath),
        ).fetchone()
        return row[0] if row else 0

    def get_ffmpeg_failure_count(self, key: str, filepath: str) -> int:
        row = self._conn.execute(
            "SELECT ffmpeg_failure_count FROM file_state WHERE task_key = ? AND filepath = ?",
            (key, filepath),
        ).fetchone()
        return row[0] if row else 0

    def get_success_state(self, key: str, filepath: str) -> SuccessState | None:
        """已成功处理过的记录快照；未处理过（无记录或仅有失败计数）返回 None。"""
        row = self._conn.execute(
            "SELECT success_time, source_size, source_mtime FROM file_state"
            " WHERE task_key = ? AND filepath = ? AND success_time IS NOT NULL",
            (key, filepath),
        ).fetchone()
        if row is None:
            return None
        return SuccessState(
            success_time=row[0],
            source_size=row[1] if row[1] is not None else 0,
            source_mtime=row[2] if row[2] is not None else 0.0,
        )

    # ---- 写入 -----------------------------------------------------------

    def increment_failure(self, key: str, filepath: str) -> int:
        """普通失败计数 +1，返回累计值。"""
        self._conn.execute(
            "INSERT INTO file_state (task_key, filepath, failure_count) VALUES (?, ?, 1)"
            " ON CONFLICT(task_key, filepath) DO UPDATE SET failure_count = failure_count + 1",
            (key, filepath),
        )
        self._conn.commit()
        return self.get_failure_count(key, filepath)

    def increment_ffmpeg_failure(self, key: str, filepath: str) -> int:
        """主命令失败计数 +1（为切换 fallback 命令累积），返回累计值。"""
        self._conn.execute(
            "INSERT INTO file_state (task_key, filepath, ffmpeg_failure_count)"
            " VALUES (?, ?, 1)"
            " ON CONFLICT(task_key, filepath)"
            " DO UPDATE SET ffmpeg_failure_count = ffmpeg_failure_count + 1",
            (key, filepath),
        )
        self._conn.commit()
        return self.get_ffmpeg_failure_count(key, filepath)

    def mark_success(
        self, key: str, filepath: str, timestamp: float, size: int, mtime: float
    ) -> None:
        """记录成功处理：写时间戳与源文件 size/mtime，双失败计数清零。"""
        self._conn.execute(
            "INSERT INTO file_state (task_key, filepath, success_time, failure_count,"
            " ffmpeg_failure_count, source_size, source_mtime)"
            " VALUES (?, ?, ?, 0, 0, ?, ?)"
            " ON CONFLICT(task_key, filepath) DO UPDATE SET"
            "    success_time = excluded.success_time,"
            "    failure_count = 0,"
            "    ffmpeg_failure_count = 0,"
            "    source_size = excluded.source_size,"
            "    source_mtime = excluded.source_mtime",
            (key, filepath, timestamp, size, mtime),
        )
        self._conn.commit()

    def delete_record(self, key: str, filepath: str) -> None:
        """删除单条记录（记录不存在时静默）。"""
        self._conn.execute(
            "DELETE FROM file_state WHERE task_key = ? AND filepath = ?", (key, filepath)
        )
        self._conn.commit()

    # ---- 生命周期 --------------------------------------------------------

    def close(self) -> None:
        # 连接已关闭等异常可安全忽略
        with contextlib.suppress(sqlite3.Error):
            self._conn.close()
