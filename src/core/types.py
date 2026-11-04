from enum import Enum


class FileAction(Enum):
    """单个被扫描文件的处理动作判定。"""

    PROCESS = "process"  # 走 FFmpeg 处理
    PASSTHROUGH = "passthrough"  # 不经 FFmpeg，直接移动到目标目录
    SKIP = "skip"  # 跳过，留在源目录


class TaskOutcome(Enum):
    """任务单轮执行的定性结果，供依赖编排判定"前置任务是否完成"。

    COMPLETED —— 本轮正常跑完（含"无事可做"的结束）；
    FAILED —— 本轮执行中断（源/目标目录缺失、停止指令等）；
    SKIPPED —— 本轮未执行（配置校验失败、装配失败等）。
    """

    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class TaskRunStats:
    """单轮任务执行的统计（print_task_summary 的数据源）。"""

    def __init__(self) -> None:
        self._processed = 0
        self._passthrough = 0
        self._failed = 0
        self._skipped = 0
        self._expired = 0
        self._evicted = 0
        self._total_bytes = 0

    @property
    def processed(self) -> int:
        return self._processed

    @property
    def passthrough(self) -> int:
        return self._passthrough

    @property
    def failed(self) -> int:
        return self._failed

    @property
    def skipped(self) -> int:
        return self._skipped

    @property
    def expired(self) -> int:
        return self._expired

    @property
    def evicted(self) -> int:
        return self._evicted

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    def record_processed(self, size_bytes: int = 0) -> None:
        self._processed += 1
        self._total_bytes += size_bytes

    def record_passthrough(self, size_bytes: int = 0) -> None:
        self._passthrough += 1
        self._total_bytes += size_bytes

    def record_failed(self) -> None:
        self._failed += 1

    def record_skipped(self) -> None:
        self._skipped += 1

    def record_expired(self) -> None:
        self._expired += 1

    def record_evicted(self) -> None:
        self._evicted += 1
