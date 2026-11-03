from enum import Enum


class FileAction(Enum):
    """单个被扫描文件的处理动作判定。"""

    PROCESS = "process"  # 走 FFmpeg 转码
    PASSTHROUGH = "passthrough"  # 不转码，直接移动到目标目录
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
