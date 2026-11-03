"""入口：前台运行与 start / log / stop / run-once 控制子命令。

- 无子命令：前台运行（读 config/config.toml）；
- start：脱离终端运行实例，随后在本终端滚动显示运行日志（窗口可随时关闭；
  --quiet 时确认实例启动后即返回，不跟随日志）；
- log：滚动显示运行日志（自动跟随跨天轮转），底部状态栏显示实例运行状态；
- stop：停止运行中的实例（--fast 立即中止处理 / --slow 本轮结束后停止）；
- run-once：让处于调度休眠期的实例立即执行一轮任务，随后滚动显示日志
  （--quiet 时确认指令受理后即返回，未受理按失败退出）。

运行路径（装配与调度循环）见 src/app/runtime.py，控制子命令见 src/app/control.py。
"""

import argparse
import sys

from src.app import control, runtime


def _ensure_utf8_stdio() -> None:
    """管道/重定向场景下 stdout/stderr 默认跟随系统 ANSI 代码页，中文会直接
    UnicodeEncodeError（如 Windows 的 cp1252）；交互控制台走 WindowsConsoleIO，
    编码本就是 UTF-8，不受影响。此处将非 UTF-8 的流统一强制为 UTF-8，
    与日志文件编码保持一致。冻结产物会忽略 PYTHONUTF8/PYTHONIOENCODING
    环境变量，因此只能在入口代码处理。"""
    for stream in (sys.stdout, sys.stderr):
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        if (stream.encoding or "").lower().replace("-", "") != "utf8":
            stream.reconfigure(encoding="utf-8")


def main() -> None:
    _ensure_utf8_stdio()

    parser = argparse.ArgumentParser(
        description="MediaConveyor - 音视频文件自动化处理工具",
        epilog="子命令: start（脱离终端运行）/ log（滚动显示日志）/ stop（停止实例）"
        "/ run-once（立即执行一轮）",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="子命令")

    start_parser = subparsers.add_parser("start", help="脱离终端运行实例，并滚动显示运行日志")
    start_parser.add_argument(
        "--quiet",
        action="store_true",
        help="不滚动显示日志，确认实例已启动（持有实例锁）后即返回；超时未确认则报错退出",
    )

    log_parser = subparsers.add_parser(
        "log", help="滚动显示运行日志（自动跟随跨天轮转的新日志文件）"
    )
    log_parser.add_argument(
        "-n",
        "--lines",
        type=int,
        default=100,
        metavar="N",
        help="初始显示的最近日志行数（默认 100）",
    )

    stop_parser = subparsers.add_parser("stop", help="停止正在运行的实例")
    stop_group = stop_parser.add_mutually_exclusive_group()
    stop_group.add_argument(
        "--fast",
        action="store_true",
        help="立即中止正在运行的 FFmpeg 处理与文件操作并退出（5 秒内未退出则强制结束）",
    )
    stop_group.add_argument("--slow", action="store_true", help="等待本轮任务结束后再退出")

    run_once_parser = subparsers.add_parser(
        "run-once", help="让处于调度休眠期的实例立即执行一轮任务，并滚动显示运行日志"
    )
    run_once_parser.add_argument(
        "--quiet",
        action="store_true",
        help="不滚动显示日志，指令受理确认后即返回；未受理则以非零码退出",
    )

    args = parser.parse_args()

    if args.command is None:
        runtime.run_client()
    elif args.command == "start":
        control.cmd_start(args.quiet)
    elif args.command == "log":
        if args.lines < 0:
            parser.error("--lines 不能为负数")
        control.cmd_log(args.lines)
    elif args.command == "stop":
        control.cmd_stop(args.fast, args.slow)
    elif args.command == "run-once":
        control.cmd_run_once(args.quiet)


if __name__ == "__main__":
    main()
