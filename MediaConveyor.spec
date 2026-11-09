# -*- mode: python ; coding: utf-8 -*-
"""MediaConveyor 打包配置（PyInstaller，onedir 模式）。

由 scripts/build_executable.py 驱动构建：脚本会先下载并暂存对应平台的
FFmpeg，再通过环境变量 FFMPEG_STAGE 把暂存目录传入本文件，ffmpeg 与
ffprobe 被打包进程序目录的 ffmpeg/ 子目录（PyInstaller 6.x onedir 布局下
位于 _internal/ffmpeg/），运行时由 src/app/paths.py 前置到 PATH。

产物布局（dist/MediaConveyor/）：
    MediaConveyor.exe        Windows 入口
    MediaConveyor            macOS 入口（无后缀裸二进制）
    _internal/               运行时依赖与内置 FFmpeg
"""

import os

ffmpeg_stage = os.environ.get("FFMPEG_STAGE")
if not ffmpeg_stage or not os.path.isdir(ffmpeg_stage):
    raise SystemExit(
        "缺少内置 FFmpeg 暂存目录：请通过 scripts/build_executable.py 构建，"
        "或设置环境变量 FFMPEG_STAGE 指向含 ffmpeg / ffprobe 的暂存目录。"
    )

# 暂存目录由构建脚本按目标平台归一命名（Windows 带 .exe 后缀）；
# PyInstaller 不支持交叉打包，本文件总是为当前平台打包，os.name 判定即可
_ffmpeg_suffix = ".exe" if os.name == "nt" else ""
ffmpeg_bin = os.path.join(ffmpeg_stage, "ffmpeg" + _ffmpeg_suffix)
ffprobe_bin = os.path.join(ffmpeg_stage, "ffprobe" + _ffmpeg_suffix)
if not (os.path.isfile(ffmpeg_bin) and os.path.isfile(ffprobe_bin)):
    raise SystemExit(f"FFmpeg 暂存目录缺少二进制: {ffmpeg_stage}")

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[(ffmpeg_bin, "ffmpeg"), (ffprobe_bin, "ffmpeg")],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MediaConveyor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="MediaConveyor",
)
