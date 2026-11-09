"""构建 MediaConveyor 可执行文件（PyInstaller onedir + 内置 FFmpeg）。

用法:
    python scripts/build_executable.py                       # 自动识别当前平台
    python scripts/build_executable.py --target macos-arm64  # 指定目标平台

流程: 下载并暂存对应平台的 FFmpeg（版本唯一出处：pyproject.toml 的
      [tool.mediaconveyor].ffmpeg_version）→ 以 MediaConveyor.spec 执行
      PyInstaller onedir 打包 → 将 GPL 许可文件与示例配置复制进程序目录
      → 压缩为发行 zip。

FFmpeg 二进制来源:
    - windows-*: BtbN/FFmpeg-Builds 的同名分支构建 zip（GitHub release
      资产，附 checksums.sha256 校验）；
    - macos-*: evermeet.cx 的静态构建（x86_64，Apple Silicon 经 Rosetta
      运行；zip，经 Info API 按分支自动探测精确版本）。

产物:
    dist/MediaConveyor/                              解包即用的程序目录
    dist/MediaConveyor-<版本>-<目标平台>.zip          发行压缩包（config/ 下附带示例配置）
"""

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
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
LICENSES_SRC = os.path.join(REPO_ROOT, "resources", "licenses")

# BtbN/FFmpeg-Builds 的滚动发布 tag（资产名携带分支号，内容随分支更新）
BTBN_ASSET_URL = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/{filename}"
# evermeet Info API：{binary}/{version} 查询精确构建（含下载直链与大小），
# {binary}/release 查询最新 release
EVERMEET_INFO_URL = "https://evermeet.cx/ffmpeg/info/{path}"
# evermeet 上按补丁号探测分支内最新构建的上界（分支最新 release 不超过 x.y.30）
EVERMEET_PROBE_PATCH_MAX = 30


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
    """加载 pyproject.toml（应用版本与内置 FFmpeg 版本的唯一出处）。"""
    with open(os.path.join(REPO_ROOT, "pyproject.toml"), "rb") as f:
        return tomllib.load(f)


def read_version(pyproject):
    """从 [project].version 读取版本号。"""
    return pyproject["project"]["version"]


def read_ffmpeg_version(pyproject):
    """从 [tool.mediaconveyor].ffmpeg_version 读取内置 FFmpeg 分支版本。"""
    return pyproject["tool"]["mediaconveyor"]["ffmpeg_version"]


def download(url, dest, retries=3, timeout=600):
    """下载到 dest（.part 暂存 + 原子替换），失败重试。"""
    for attempt in range(1, retries + 1):
        try:
            print(f"下载（第 {attempt}/{retries} 次）: {url}")
            part = dest + ".part"
            request = urllib.request.Request(url, headers={"User-Agent": "MediaConveyor-build"})
            with urllib.request.urlopen(request, timeout=timeout) as resp, open(part, "wb") as f:
                f.write(resp.read())
            os.replace(part, dest)
            return
        except Exception as e:  # noqa: BLE001
            if attempt == retries:
                raise SystemExit(f"下载失败: {url}\n{e}")
            print(f"下载失败，将重试: {e}")


def stage_dir_for(target, ffmpeg_version):
    """FFmpeg 暂存目录：build/ffmpeg/v{版本}/{target}，按版本与平台隔离。"""
    return os.path.join(REPO_ROOT, "build", "ffmpeg", f"v{ffmpeg_version}", target)


def btbn_filename(target, ffmpeg_version):
    """BtbN 分支构建的资产文件名（Windows 双架构）。"""
    arch = "win64" if target == "windows-amd64" else "winarm64"
    return f"ffmpeg-n{ffmpeg_version}-latest-{arch}-gpl-{ffmpeg_version}.zip"


def verify_sha256(archive_path, filename):
    """下载 BtbN 发布的 checksums.sha256 并校验压缩包完整性。"""
    sums_path = archive_path + ".sha256"
    download(BTBN_ASSET_URL.format(filename="checksums.sha256"), sums_path, timeout=60)
    expected = None
    with open(sums_path, encoding="utf-8") as f:
        for line in f:
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            # sha256sum 格式：<hash>  <name>（二进制模式为 <hash> *<name>）
            name = parts[1].strip().lstrip("*")
            if name == filename or os.path.basename(name) == filename:
                expected = parts[0].strip().lower()
                break
    os.remove(sums_path)
    if expected is None:
        raise SystemExit(f"checksums.sha256 中未找到 {filename} 的校验和。")

    digest = hashlib.sha256()
    with open(archive_path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected:
        raise SystemExit(f"{filename} 校验和不匹配（BtbN 资产可能刚滚动更新），请重新执行构建。")


def extract_member(archive, member_name, stage_dir, staged_path):
    """解出压缩包内的单个文件并移动到暂存路径。"""
    with zipfile.ZipFile(archive) as zf:
        member = next(n for n in zf.namelist() if os.path.basename(n) == member_name)
        zf.extract(member, stage_dir)
    shutil.move(os.path.join(stage_dir, member), staged_path)


def stage_windows_ffmpeg(target, ffmpeg_version):
    """下载并解包 BtbN 分支构建，返回暂存目录（已暂存则复用）。"""
    filename = btbn_filename(target, ffmpeg_version)
    stage_dir = stage_dir_for(target, ffmpeg_version)
    os.makedirs(stage_dir, exist_ok=True)
    staged = [os.path.join(stage_dir, name) for name in ("ffmpeg.exe", "ffprobe.exe")]

    if all(os.path.isfile(path) for path in staged):
        print(f"复用已暂存的 FFmpeg: {stage_dir}")
        return stage_dir

    zip_path = os.path.join(stage_dir, "ffmpeg.zip")
    download(BTBN_ASSET_URL.format(filename=filename), zip_path)
    verify_sha256(zip_path, filename)
    for exe in ("ffmpeg.exe", "ffprobe.exe"):
        extract_member(zip_path, exe, stage_dir, os.path.join(stage_dir, exe))
    os.remove(zip_path)

    print(f"FFmpeg 已暂存: {stage_dir}")
    return stage_dir


def evermeet_info(path):
    """查询 evermeet Info API（path 为 info/ 之后的路径），返回 JSON。"""
    url = EVERMEET_INFO_URL.format(path=path)
    request = urllib.request.Request(url, headers={"User-Agent": "MediaConveyor-build"})
    with urllib.request.urlopen(request, timeout=60) as resp:
        return json.load(resp)


def resolve_evermeet_version(ffmpeg_version):
    """把分支版本（如 9.0）解析为 evermeet 上的精确版本，返回 (版本, ffmpeg info)。

    优先采用 evermeet 的最新 release（通常即目标分支）；其版本不在目标
    分支内时，按补丁号自高向低探测分支内最新的可用构建，最后尝试裸
    分支号本身（上游存在 x.y 形式的 release tag）。
    """
    release = evermeet_info("ffmpeg/release")
    version = str(release.get("version", ""))
    if version == ffmpeg_version or version.startswith(f"{ffmpeg_version}."):
        return version, release

    for patch in range(EVERMEET_PROBE_PATCH_MAX, 0, -1):
        candidate = f"{ffmpeg_version}.{patch}"
        try:
            info = evermeet_info(f"ffmpeg/{candidate}")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            raise
        return str(info["version"]), info

    try:
        info = evermeet_info(f"ffmpeg/{ffmpeg_version}")
    except urllib.error.HTTPError as e:
        raise SystemExit(
            f"evermeet.cx 上未找到分支 {ffmpeg_version} 的任何构建"
            f"（最新 release 为 {version}，探测至裸版本号均未命中: HTTP {e.code}）。"
        ) from e
    return str(info["version"]), info


def stage_macos_ffmpeg(target, ffmpeg_version):
    """下载并解包 evermeet 的 ffmpeg / ffprobe，返回暂存目录（已暂存则复用）。"""
    stage_dir = stage_dir_for(target, ffmpeg_version)
    os.makedirs(stage_dir, exist_ok=True)
    staged = {name: os.path.join(stage_dir, name) for name in ("ffmpeg", "ffprobe")}

    if all(os.path.isfile(path) for path in staged.values()):
        print(f"复用已暂存的 FFmpeg: {stage_dir}")
        return stage_dir

    version, ffmpeg_info = resolve_evermeet_version(ffmpeg_version)
    print(f"evermeet 构建版本: {version}")

    for binary, staged_path in staged.items():
        # ffprobe 与 ffmpeg 同版本构建，分别经 Info API 取下载直链
        info = ffmpeg_info if binary == "ffmpeg" else evermeet_info(f"ffprobe/{version}")
        zip_meta = info["download"]["zip"]
        zip_path = os.path.join(stage_dir, f"{binary}.zip")
        download(zip_meta["url"], zip_path)

        expected_size = zip_meta.get("size")
        if expected_size is not None and os.path.getsize(zip_path) != expected_size:
            raise SystemExit(f"{binary}.zip 大小与 Info API 不一致，下载可能不完整。")

        extract_member(zip_path, binary, stage_dir, staged_path)
        os.remove(zip_path)
        os.chmod(staged_path, 0o755)

    print(f"FFmpeg 已暂存: {stage_dir}")
    return stage_dir


def stage_ffmpeg(target, ffmpeg_version):
    """下载并暂存对应平台的 FFmpeg（ffmpeg + ffprobe），返回暂存目录。"""
    if target.startswith("windows"):
        return stage_windows_ffmpeg(target, ffmpeg_version)
    if target.startswith("macos"):
        return stage_macos_ffmpeg(target, ffmpeg_version)
    raise SystemExit(f"不支持的目标平台: {target}")


def target_matches_platform(target):
    """构建目标是否与当前运行平台一致（跨平台暂存时无法运行对方二进制）。"""
    return (
        platform.system() == "Windows"
        if target.startswith("windows")
        else platform.system() == "Darwin"
    )


def verify_ffmpeg(stage_dir, target, ffmpeg_version):
    """执行 ffmpeg / ffprobe -version，冒烟校验并断言版本属于目标分支。"""
    exe_suffix = ".exe" if target.startswith("windows") else ""
    for name in ("ffmpeg", "ffprobe"):
        binary = os.path.join(stage_dir, name + exe_suffix)
        try:
            result = subprocess.run(
                [binary, "-version"],
                capture_output=True,
                text=True,
                errors="replace",
                check=True,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as e:
            raise SystemExit(f"内置 {name} 无法运行: {e}")

        first_line = result.stdout.splitlines()[0] if result.stdout else ""
        if ffmpeg_version not in first_line:
            raise SystemExit(f"内置 {name} 的版本输出不含目标分支 {ffmpeg_version}: {first_line}")
        print(f"内置 {name} 校验通过: {first_line}")


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


def stage_licenses():
    """将 GPL 许可文件复制进程序目录（内置 FFmpeg 为 GPL 构建，须随包提供
    许可文本与源码获取说明，见 resources/licenses/）。"""
    if not os.path.isdir(LICENSES_SRC):
        raise SystemExit(f"缺少许可文件目录: {LICENSES_SRC}")
    licenses_dst = os.path.join(REPO_ROOT, "dist", "MediaConveyor", "licenses")
    shutil.copytree(LICENSES_SRC, licenses_dst, dirs_exist_ok=True)


def run_pyinstaller(ffmpeg_stage):
    env = os.environ.copy()
    env["FFMPEG_STAGE"] = ffmpeg_stage

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        SPEC_FILE,
        "--noconfirm",
        "--clean",
    ]
    print("+ " + " ".join(cmd))
    subprocess.run(cmd, cwd=REPO_ROOT, env=env, check=True)


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
    ffmpeg_version = read_ffmpeg_version(pyproject)
    print(f"构建目标: {target}，版本: {version}，内置 FFmpeg: {ffmpeg_version}")

    ffmpeg_stage = stage_ffmpeg(target, ffmpeg_version)
    if target_matches_platform(target):
        verify_ffmpeg(ffmpeg_stage, target, ffmpeg_version)
    else:
        print("目标平台与当前平台不同，跳过内置 FFmpeg 的运行校验（仅暂存）。")

    clean_dist()
    run_pyinstaller(ffmpeg_stage)
    stage_example_configs()
    stage_licenses()
    zip_path = make_zip(target, version)

    print(
        f"构建完成:\n  程序目录: dist{os.sep}MediaConveyor\n  发行包: {os.path.relpath(zip_path, REPO_ROOT)}"
    )


if __name__ == "__main__":
    main()
