"""构建 MediaConveyor 可执行文件（PyInstaller onedir）。

用法:
    python scripts/build_executable.py                       # 自动识别当前平台
    python scripts/build_executable.py --target macos-arm64  # 指定目标平台

流程: 以 MediaConveyor.spec 执行 PyInstaller onedir 打包
      → 将示例配置复制进程序目录 → 压缩为发行 zip。

FFmpeg 不打入包内：运行要求 PATH 中已安装 ffmpeg / ffprobe。

产物:
    dist/MediaConveyor/                              解包即用的程序目录
    dist/MediaConveyor-<版本>-<目标平台>.zip          发行压缩包（config/ 下附带示例配置）
"""

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tomllib
import zipfile

# 支持的构建目标（Windows/macOS × AMD64/ARM64）
TARGETS = (
    "macos-amd64",
    "macos-arm64",
    "windows-amd64",
    "windows-arm64",
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC_FILE = os.path.join(REPO_ROOT, "MediaConveyor.spec")


def detect_target():
    """从当前运行环境识别构建目标。"""
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "amd64"

    system = platform.system()
    if system == "Windows":
        return f"windows-{arch}"
    if system == "Darwin":
        return f"macos-{arch}"

    raise SystemExit(
        f"不支持的平台: {system} {machine}。"
        f"可执行文件仅支持 Windows/macOS，可用 --target 显式指定（仅用于交叉暂存）。"
    )


def read_pyproject():
    """加载 pyproject.toml（应用版本的唯一出处）。"""
    with open(os.path.join(REPO_ROOT, "pyproject.toml"), "rb") as f:
        return tomllib.load(f)


def read_version(pyproject):
    """从 [project].version 读取版本号。"""
    return pyproject["project"]["version"]


def clean_dist():
    """清空上一次构建产物，避免手动运行残留（config.toml、logs 等）混入发行包。"""
    dist_dir = os.path.join(REPO_ROOT, "dist", "MediaConveyor")
    if os.path.isdir(dist_dir):
        shutil.rmtree(dist_dir)


def stage_example_configs():
    """将仓库 config/ 下的示例配置复制进程序目录，供用户改名后使用。"""
    config_src = os.path.join(REPO_ROOT, "config")
    config_dst = os.path.join(REPO_ROOT, "dist", "MediaConveyor", "config")
    os.makedirs(config_dst, exist_ok=True)
    for name in os.listdir(config_src):
        if name.endswith(".example.toml"):
            shutil.copy2(os.path.join(config_src, name), os.path.join(config_dst, name))


def run_pyinstaller():
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        SPEC_FILE,
        "--noconfirm",
        "--clean",
    ]
    print("+ " + " ".join(cmd))
    subprocess.run(cmd, cwd=REPO_ROOT, check=True)


def make_zip(target, version):
    """压缩 dist/MediaConveyor 为发行 zip（保留可执行权限位）。"""
    dist_dir = os.path.join(REPO_ROOT, "dist", "MediaConveyor")
    if not os.path.isdir(dist_dir):
        raise SystemExit(f"打包产物缺失: {dist_dir}")

    zip_path = os.path.join(REPO_ROOT, "dist", f"MediaConveyor-{version}-{target}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(dist_dir):
            for name in files:
                full = os.path.join(root, name)
                arc = os.path.relpath(full, os.path.dirname(dist_dir))
                arc = arc.replace(os.sep, "/")
                zf.write(full, arc)
                # 显式标记为 0755，保证 macOS/Linux 解压后二进制可直接执行
                zf.getinfo(arc).external_attr = 0o100755 << 16

    return zip_path


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="构建 MediaConveyor 可执行文件")
    parser.add_argument(
        "--target",
        choices=TARGETS,
        help="目标平台，缺省自动识别当前平台",
    )
    args = parser.parse_args()

    target = args.target or detect_target()
    pyproject = read_pyproject()
    version = read_version(pyproject)
    print(f"构建目标: {target}，版本: {version}")

    clean_dist()
    run_pyinstaller()
    stage_example_configs()
    zip_path = make_zip(target, version)

    print(
        f"构建完成:\n  程序目录: dist{os.sep}MediaConveyor\n  发行包: {os.path.relpath(zip_path, REPO_ROOT)}"
    )


if __name__ == "__main__":
    main()
