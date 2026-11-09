# 内置 FFmpeg：许可与源码获取说明

本发行包内置的 `ffmpeg` 与 `ffprobe` 为 FFmpeg 项目的静态 GPL 构建，
遵循 GNU GPLv3 许可证（全文见同目录 `FFMPEG-LICENSE-GPLv3.txt`）。
MediaConveyor 本体以 MIT 许可发行，内置 FFmpeg 不改变其许可。

## 版本与来源

- 目标版本由 MediaConveyor 仓库 `pyproject.toml` 的
  `[tool.mediaconveyor].ffmpeg_version` 统一声明（语义为 FFmpeg 发布分支），
  由构建脚本 `scripts/build_executable.py` 下载、校验并打包。
- Windows：[BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds)
  的同名分支构建（GPL 变体，下载时以发布资产的 `checksums.sha256` 校验）。
- macOS：[evermeet.cx](https://evermeet.cx/ffmpeg/) 提供的静态构建
  （Intel x86_64；Apple Silicon 上经 Rosetta 2 运行）。

## 完整对应源码（GPLv3 第 6 条）

- FFmpeg 各版本源码：<https://ffmpeg.org/download.html>
  （git 仓库：<https://git.ffmpeg.org/ffmpeg.git>）
- BtbN 构建脚本与补丁：<https://github.com/BtbN/FFmpeg-Builds/tree/master/build>
