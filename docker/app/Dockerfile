# syntax=docker/dockerfile:1
FROM python:3.14-slim-trixie

# 设置环境变量，确保 Python 输出不被缓冲，以便日志能实时显示
ENV PYTHONUNBUFFERED=1

# 启用 non-free 和 contrib 仓库以安装硬件加速驱动
RUN if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
    sed -i 's/Components: main.*/Components: main contrib non-free non-free-firmware/g' /etc/apt/sources.list.d/debian.sources; \
    else \
    sed -i 's/main/main contrib non-free non-free-firmware/g' /etc/apt/sources.list; \
    fi

# 安装 GPU 加速驱动与静态构建的 FFmpeg（Debian 源的 ffmpeg 版本较旧，且不带
# 完整的 VAAPI/NVENC 支持）：
# - intel-media-va-driver-non-free: Intel QSV/VAAPI（仅 amd64）
# - mesa-va-drivers: AMD VAAPI
# - nvidia-vaapi-driver: NVIDIA VAAPI（仅 amd64）
# - FFmpeg 取 BtbN 的 n8.1 静态构建（amd64/arm64 双架构分支）
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    vainfo \
    mesa-va-drivers \
    wget \
    xz-utils && \
    ARCH=$(dpkg --print-architecture) && \
    if [ "$ARCH" = "amd64" ]; then \
    apt-get install -y --no-install-recommends \
    intel-media-va-driver-non-free \
    nvidia-vaapi-driver; \
    FFMPEG_URL="https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n8.1-latest-linux64-gpl-8.1.tar.xz"; \
    FFMPEG_DIR="ffmpeg-n8.1-latest-linux64-gpl-8.1"; \
    elif [ "$ARCH" = "arm64" ]; then \
    FFMPEG_URL="https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n8.1-latest-linuxarm64-gpl-8.1.tar.xz"; \
    FFMPEG_DIR="ffmpeg-n8.1-latest-linuxarm64-gpl-8.1"; \
    fi && \
    wget -qO /tmp/ffmpeg.tar.xz "$FFMPEG_URL" && \
    tar -xf /tmp/ffmpeg.tar.xz -C /tmp && \
    chmod +x /tmp/$FFMPEG_DIR/bin/* && \
    cp -a /tmp/$FFMPEG_DIR/bin/* /usr/local/bin/ && \
    rm -rf /tmp/ffmpeg.tar.xz /tmp/$FFMPEG_DIR && \
    rm -rf /var/lib/apt/lists/*

# 设置工作目录
WORKDIR /app

# 安装依赖层（源码变更时此层缓存仍可复用）：
# uv 通过 BuildKit bind mount 从官方镜像临时挂载，仅本条 RUN 期间可见、不落入
# 任何镜像层（最终镜像不含 uv）；下载缓存经 cache mount 存于构建器而非镜像层
# （cache 目录按 key 持久复用，重建此层时免重新下载）。钉精确版本，与本地生成
# uv.lock 的 uv 版本一致，dependabot 不跟踪该引用，升级 uv 重锁后需手动同步此处。
# --locked 严格按 uv.lock 安装，锁与 pyproject.toml 不一致时构建失败；
# --only-group runtime 只装运行依赖组（uv sync 默认会装 dev 组）；
# --no-install-project 项目无 [build-system]（虚拟项目），显式声明不安装项目本身；
# UV_COMPILE_BYTECODE=1 预编译字节码，对齐 pip 安装的默认行为，首次启动免编译
COPY pyproject.toml uv.lock ./
RUN --mount=from=ghcr.io/astral-sh/uv:0.13.5,source=/uv,target=/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    UV_COMPILE_BYTECODE=1 uv sync --locked --only-group runtime --no-install-project

# venv 中的解释器与依赖优先于系统 Python（CMD 的 python 解析到 venv）
ENV PATH="/app/.venv/bin:$PATH"

# 复制项目文件
COPY . .

# NVIDIA GPU 支持环境变量（配合 docker-compose 的 deploy.resources 段）
ENV NVIDIA_VISIBLE_DEVICES=all
ENV NVIDIA_DRIVER_CAPABILITIES=compute,video,utility

# 默认前台运行；docker-compose 以 start 子命令拉起分离式实例
CMD ["python", "main.py"]
