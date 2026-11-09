"""对 MediaConveyor 发行 zip 做冒烟检查：包结构 + 入口可运行。

用法:
    python scripts/smoke_test.py dist/MediaConveyor-1.2.3-windows-amd64.zip
    python scripts/smoke_test.py "dist/MediaConveyor-*-windows-amd64.zip"  # 支持通配符（须唯一命中）

检查项:
    1. zip 内入口与内置 FFmpeg 二进制带可执行权限位——make_zip 的契约：
       全部条目统一标记 0755；
    2. 解压后包结构完整：入口二进制、_internal/ffmpeg/ 内置 FFmpeg、
       licenses/ 许可文件、config/ 示例配置；
    3. 运行入口 --help 并断言正常退出。argparse 在读取配置之前处理 --help，
       冻结形态下无需 config.toml 即可启动，恰好覆盖"二进制能起、
       全部 import 可解析、入口正常"这一构建退出码无法证明的链路；
    4. 运行内置 ffmpeg / ffprobe -version，断言可执行。

说明: Python 的 zip 解压不还原 Unix 权限位（用户解压工具会还原），
    因此非 Windows 平台在运行前显式补 0755，等价于用户的真实解压结果。

全部通过则退出码 0；任何一项失败以非 0 退出并给出中文错误信息。
"""

import argparse
import glob
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile


def entry_name():
    """打包入口名：Windows 为 MediaConveyor.exe，其余平台为裸二进制。"""
    return "MediaConveyor.exe" if os.name == "nt" else "MediaConveyor"


def ffmpeg_names():
    """内置 FFmpeg 的二进制名（zip 内位于 MediaConveyor/_internal/ffmpeg/ 下）。"""
    return ("ffmpeg.exe", "ffprobe.exe") if os.name == "nt" else ("ffmpeg", "ffprobe")


def locate_zip(pattern):
    """把参数解析为唯一的发行包路径（支持通配符，跨 shell 行为一致）。"""
    matches = sorted(glob.glob(pattern))
    if not matches:
        raise SystemExit(f"未找到发行包: {pattern}")
    if len(matches) > 1:
        raise SystemExit("匹配到多个发行包，请传入具体路径:\n  " + "\n  ".join(matches))
    return matches[0]


def check_exec_bits(zip_path):
    """校验 zip 关键条目的可执行权限位（make_zip 对全部条目统一标记 0755）。"""
    targets = {entry_name(), *ffmpeg_names()}
    problems = []
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if os.path.basename(info.filename) not in targets:
                continue
            if not (info.external_attr >> 16) & 0o111:
                problems.append(f"zip 内 {info.filename} 缺少可执行权限位")
    return problems


def extract_package(zip_path, dest_dir):
    """解压发行包，返回程序目录路径（发行包顶层为 MediaConveyor/）。"""
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest_dir)
    program_dir = os.path.join(dest_dir, "MediaConveyor")
    if not os.path.isdir(program_dir):
        raise SystemExit("发行包顶层缺少 MediaConveyor/ 目录。")
    return program_dir


def check_package_layout(program_dir):
    """校验解压后的包结构完整性，返回问题列表。"""
    problems = []
    if not os.path.isfile(os.path.join(program_dir, entry_name())):
        problems.append(f"缺少入口二进制: MediaConveyor/{entry_name()}")

    ffmpeg_dir = os.path.join(program_dir, "_internal", "ffmpeg")
    for name in ffmpeg_names():
        if not os.path.isfile(os.path.join(ffmpeg_dir, name)):
            problems.append(f"缺少内置 FFmpeg 二进制: MediaConveyor/_internal/ffmpeg/{name}")

    licenses_dir = os.path.join(program_dir, "licenses")
    if not os.path.isfile(os.path.join(licenses_dir, "FFMPEG-LICENSE-GPLv3.txt")):
        problems.append("缺少内置 FFmpeg 的许可文件: licenses/FFMPEG-LICENSE-GPLv3.txt")

    config_dir = os.path.join(program_dir, "config")
    examples = []
    if os.path.isdir(config_dir):
        examples = [n for n in os.listdir(config_dir) if n.endswith(".example.toml")]
    if not examples:
        problems.append("config/ 下缺少示例配置（*.example.toml）")
    return problems


def ensure_runnable(program_dir):
    """补齐执行位（Python 解压不还原权限位，用户解压工具会还原）。"""
    if os.name == "nt":
        return
    paths = [
        os.path.join(program_dir, entry_name()),
        *(os.path.join(program_dir, "_internal", "ffmpeg", name) for name in ffmpeg_names()),
    ]
    for path in paths:
        if os.path.isfile(path):
            os.chmod(path, 0o755)


def run_entry_help(program_dir):
    """运行入口 --help，断言正常退出且输出非空。"""
    entry = os.path.join(program_dir, entry_name())
    env = os.environ.copy()
    # 非冻结运行时固定 UTF-8；冻结产物会忽略 PYTHON* 环境变量，
    # 由入口的 _ensure_utf8_stdio 保证输出编码，避免控制台差异造成误报
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    cmd = [entry, "--help"]
    print("+ " + " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            check=False,
            cwd=program_dir,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        raise SystemExit("入口二进制 --help 运行超时（120 秒）。")

    if result.returncode != 0 or not result.stdout.strip():
        print("---- stdout ----")
        print(result.stdout or "(空)")
        print("---- stderr ----")
        print(result.stderr or "(空)")
        raise SystemExit(f"入口二进制启动失败，退出码: {result.returncode}")

    print(f"入口校验通过: {result.stdout.splitlines()[0]}")


def run_bundled_ffmpeg(program_dir):
    """运行内置 ffmpeg / ffprobe -version，断言正常退出（版本横幅首行即校验输出）。"""
    ffmpeg_dir = os.path.join(program_dir, "_internal", "ffmpeg")
    for name in ffmpeg_names():
        cmd = [os.path.join(ffmpeg_dir, name), "-version"]
        print("+ " + " ".join(cmd))
        try:
            result = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            raise SystemExit(f"内置 {name} -version 运行超时（30 秒）。")

        if result.returncode != 0 or not result.stdout.strip():
            print("---- stdout ----")
            print(result.stdout or "(空)")
            print("---- stderr ----")
            print(result.stderr or "(空)")
            raise SystemExit(f"内置 {name} 无法运行，退出码: {result.returncode}")

        print(f"内置 {name} 校验通过: {result.stdout.splitlines()[0]}")


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="对 MediaConveyor 发行 zip 做冒烟检查")
    parser.add_argument("zip_file", help="发行 zip 路径，支持通配符（须唯一命中）")
    args = parser.parse_args()

    zip_path = locate_zip(args.zip_file)
    print(f"冒烟目标: {zip_path}")

    problems = check_exec_bits(zip_path)

    tmp_dir = tempfile.mkdtemp(prefix="mediaconveyor-smoke-")
    try:
        program_dir = extract_package(zip_path, tmp_dir)
        problems += check_package_layout(program_dir)
        for problem in problems:
            print(f"[缺失] {problem}")
        if problems:
            raise SystemExit("包结构检查失败。")

        ensure_runnable(program_dir)
        run_entry_help(program_dir)
        run_bundled_ffmpeg(program_dir)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    print("冒烟检查通过。")


if __name__ == "__main__":
    main()
