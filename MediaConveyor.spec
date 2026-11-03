# -*- mode: python ; coding: utf-8 -*-
"""MediaConveyor 打包配置（PyInstaller，onedir 模式）。

由 scripts/build_executable.py 驱动构建。FFmpeg 不打入包内：
可执行文件运行要求 PATH 中已安装 ffmpeg / ffprobe。

产物布局（dist/MediaConveyor/）：
    MediaConveyor.exe        Windows 入口
    MediaConveyor            macOS 入口（无后缀裸二进制）
    _internal/               运行时依赖
"""

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
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
