# MediaConveyor

**MediaConveyor** 是一个基于 Python 的轻量级自动化工具，用于监听指定目录中的音视频文件，并在文件写入完成后自动调用 FFmpeg 进行处理。它非常适合需要自动化音视频转码、压缩或格式转换的场景。

📝 [更新日志](./CHANGELOG.md)

> 本项目基于 [SmartArchiver](https://github.com/HexStan/SmartArchiver) 的常驻任务骨架重构而来，继承了其调度、实例控制与工程化设施；领域功能自 MediaConveyorV1 移植。配置格式与 V1 不兼容，迁移说明见 [CHANGELOG](./CHANGELOG.md)。

## 💡 启发与背景

该项目受到 [jlesage/docker-handbrake](https://github.com/jlesage/docker-handbrake) 项目中的 `autovideoconverter` 功能的启发，借鉴了其核心逻辑。由于 HandBrake 对于 Linux 上 VAAPI 和 AMD GPU 私有驱动的支持不佳，所以我开发了此项目。

## ✨ 特性

- **目录监听**：支持持续监听（固定间隔 / cron）或作为一次性脚本运行。
- **多任务支持**：在单个配置文件中定义多个独立的监听和处理任务，支持任务依赖（`depends_on`）。
- **文件稳定性检测**：通过检测文件修改时间和大小变化，确保只处理已完全写入的文件，避免处理正在下载或复制中的不完整文件。
- **文件过滤**：按文件名（fnmatch 流水线）与媒体属性（时长/码率/帧率/分辨率/编码，经 ffprobe）筛选文件。
- **自定义 FFmpeg 命令**：支持完全自定义的 FFmpeg 处理命令与备用命令（自动切换），灵活应对各种音视频处理需求。
- **状态管理**：SQLite 状态库记录处理状态与失败计数，防止重复处理；源文件内容变化后自动重新处理。
- **源文件善后**：处理完成后按配置立即删除、延迟删除、备份归档或保留在原地。
- **实例控制**：`start` / `log` / `stop` / `run-once` 子命令管理后台实例；单实例锁在 Windows / macOS / Linux 上均有效。
- **平滑停止**：`stop --fast` 立即中止（FFmpeg 子进程整树终止，不留孤儿）/ 默认完成当前文件 / `--slow` 完成本轮。
- **配置热重载**：修改 `config.toml` 后自动生效，加载失败回退备份配置。
- **Docker 支持**：镜像内置 FFmpeg 与 VAAPI/NVIDIA 驱动，支持 GPU 加速。

## 🛠️ 依赖要求

- **[uv](https://docs.astral.sh/uv/)**：用于按锁文件安装依赖并运行程序（Python 由 uv 托管，无需单独安装）。
- **FFmpeg**：必须安装并配置在系统的环境变量（PATH）中（Docker 镜像已内置）。

## 🚀 安装与运行

### 方式一：直接运行

1. **克隆仓库**

   ```bash
   git clone https://github.com/HexStan/MediaConveyor.git
   cd MediaConveyor
   ```

2. **安装依赖**

   ```bash
   uv sync --all-groups
   ```

3. **准备配置文件**

   ```bash
   cp config/config.example.toml config/config.toml
   ```

4. **运行程序**

   ```bash
   uv run python main.py                   # 前台运行（无 [schedule] 时扫描一轮后退出）
   uv run python main.py start             # 分离式后台实例，并滚动显示日志
   uv run python main.py stop              # 停止后台实例
   uv run python main.py run-once          # 让休眠中的实例立即执行一轮
   ```

### 方式二：使用 Docker

1. **准备配置文件**

   ```bash
   cp config/config.example.toml config/config.toml
   ```

2. **使用 Docker Compose 启动**

   ```bash
   docker-compose up -d
   ```

## 🧭 配置向导

本节以决策树的形式，逐步引导你根据需求组装 `config/config.toml` 配置文件。请从头到尾依次回答下列问题，将对应配置项添加到配置文件中。

---

### 第一步：选择运行模式

**你希望程序如何运行？**

| 选项 | 说明 | 配置 |
|------|------|------|
| **A. 一次性执行** | 扫描目录一次，处理完所有符合条件的文件后自动退出。适合手动触发或配合定时任务（如 cron）使用。 | 省略整个 `[schedule]` 节 |
| **B. 固定间隔监听** | 程序长期运行，每隔固定时间扫描一次目录，自动处理新增文件。适合作为后台服务。 | `[schedule]` 节：`mode = "interval"` + `interval_seconds`（秒） |
| **C. cron 定时监听** | 按 cron 表达式定时扫描（如每小时、每天凌晨）。 | `[schedule]` 节：`mode = "cron"` + `cron_expr`（5 段表达式） |

> 选择 B/C 后程序将常驻运行，可用 `stop` 子命令停止；处于调度休眠期时可用 `run-once` 立即触发一轮。

`[schedule]` 节还支持一个可选的 `first_run_delay`（首轮延迟）：`-1`（默认）等待至下个调度时间点；`0` 启动后立即执行；正数等待指定秒数后执行首轮。

### 第二步：基础配置

以下选项写在配置文件顶层：

```toml
# instance_name = "mediaconveyor"   # 【选填】实例标识名（锁文件/日志前缀基名），缺省 mediaconveyor
# log_dir = "./logs"                # 【选填】日志目录，默认 ./logs
# data_dir = "./data"               # 【选填】状态数据库目录，默认 ./data
# max_log_files = 7                 # 【选填】最多保留 N 天的日志文件，默认 0（不清理）
# log_level = "INFO"                # 【选填】日志级别：DEBUG / INFO / WARNING / ERROR / CRITICAL
```

> 排查问题时可将 `log_level` 设为 `"DEBUG"`，日志会输出详细的文件跳过原因。

### 第三步：创建任务

每个 `[[tasks]]` 节定义一个监听与处理任务。源目录、目标目录与 FFmpeg 命令为必填：

```toml
[[tasks]]
# id = "anime"                              # 【选填】任务标识，供其他任务 depends_on 引用
# name = "我的任务"                          # 【选填】任务名称，用于日志标识
source_dir = "./source"                     # 【必填】源目录（递归扫描）
dest_dir = "./dest"                         # 【必填】目标目录（保持子目录结构）
ffmpeg_cmd = """
ffmpeg -y \
  -i "{input}" \
  -c:v libx264 \
  -preset fast \
  -crf 23 \
  "{output}-encoded.mp4"
"""                                          # 【必填】FFmpeg 命令模板
```

> **占位符说明**：`{input}` → 源文件完整路径；`{output}` → 目标目录下保持子目录结构的基础文件名（不含扩展名）。命令支持多行书写，执行时自动合并为单行；路径要加引号。模板中不能出现 `{input}`/`{output}` 之外的花括号占位符。

### 第四步：选择源文件去向

**处理完成后，你希望源文件如何处理？**

| 去向 | 何时使用 | 配置 |
|------|----------|------|
| **A1. 立即删除** | 不需要保留源文件，节省空间。 | `remove_source = true`<br>`source_expired_minutes = 0` |
| **A2. 延迟删除** | 保留一段时间方便核对结果，到时自动删除。 | `remove_source = true`<br>`source_expired_minutes = 60`（分钟） |
| **B. 备份归档** | 处理成功后自动移到备份目录保存（保持子目录结构）。 | `remove_source = false`<br>`backup_dir = "./backup"` |
| **C. 保留在原地** | 不做任何移动或删除，源文件留在原处。 | `remove_source = false`<br>`# 不设置 backup_dir` |

> 选择 A1/A2 时 `backup_dir` 会被忽略（启动时记录警告）；`backup_dir` 不得位于源/目标目录之内。

### 第五步：文件稳定性检测

**源目录中的文件是否可能正在被写入（例如正在下载、拷贝中）？**

| 场景 | 配置 |
|------|------|
| **是**，文件可能处于写入中 | `stable_duration = 5`（等待 5 秒后复查文件大小是否变化。有变化则跳过，本轮不处理，按需调整秒数） |
| **否**，所有文件都是完整的 | `stable_duration = 0`（默认） |

### 第六步：文件过滤（可选）

**你需要根据文件名或文件属性筛选要处理的文件吗？**

不设置任何过滤条件时，程序处理源目录中的所有文件。无需筛选则 **跳至第七步**。

以下条件按需添加，均在 `[tasks.filter]` 下：

**文件名匹配（fnmatch 流水线）：**

三个 pattern 均使用 [fnmatch](https://docs.python.org/zh-cn/3/library/fnmatch.html) 语法匹配相对于 `source_dir` 的文件路径（不区分大小写，路径分隔符统一为 `/`），按以下顺序流水线过滤：

1. 匹配 `include_patterns`？匹配则进入下一步，不匹配则跳过（不填或为空则默认包含所有文件）。
2. 匹配 `exclude_patterns`？匹配则跳过，不匹配则进入下一步。
3. 匹配 `passthrough_patterns`？匹配则不经过 FFmpeg 直接移动到目标目录，不匹配则进入后续的属性过滤与 FFmpeg 处理。

```toml
include_patterns = [ "alpha/*", "bravo*/*", "*charlie*.mp4" ]
# "alpha/*" 匹配名称为 alpha 的目录下的所有文件（包括所有层级子目录）
# "bravo*/*" 匹配名称以 bravo 开头的目录下的所有文件
# "*charlie*.mp4" 匹配路径包含 charlie 且以 .mp4 结尾的所有文件

exclude_patterns = ["*.tmp"]                 # 匹配到的文件将被跳过

passthrough_patterns = ["*.srt", "*.jpg"]    # 匹配到的文件（如字幕、封面图）不经过 FFmpeg，直接移动
```

> `passthrough_patterns` 匹配的文件不会触发 ffprobe，也不会执行 FFmpeg 命令，但仍受 `file_mtime` 约束。

**文件与媒体属性（范围型条件均为 `{ min = ..., max = ... }`，省略的边界不限）：**

| 筛选维度 | 说明 | 示例配置 |
|----------|------|----------|
| **修改时间** | 跳过刚创建/修改的文件，确保文件已"冷却"。`0` 表示不筛选。 | `file_mtime = 300`（5 分钟前） |
| **文件大小** | 只处理特定大小范围。支持 `"100MB"`、`"1.5GiB"` 等人类可读格式。`0` 表示不限。 | `size = { min = "100MB", max = "20GB" }` |
| **视频时长** | 按媒体时长筛选。支持 `"10s"`、`"2h"`、`"1h30m"` 等写法。 | `duration = { min = "10s", max = "2h" }` |
| **视频码率** | 按视频流的码率筛选。十进制单位（1K = 1000bps）。 | `video_bitrate = { min = "500K" }` |
| **音频码率** | 按音频流的码率筛选。 | `audio_bitrate = { min = "64K", max = "320K" }` |
| **总码率** | 按媒体总码率筛选。 | `total_bitrate = { min = "1M", max = "50M" }` |
| **帧率** | 按视频帧率筛选。 | `framerate = { min = 24, max = 60 }` |
| **分辨率（短边）** | 按视频短边像素数筛选，如 `720` 表示 ≥720p。 | `short_side = { min = 720, max = 2160 }` |
| **排除视频编码** | 跳过包含指定视频编码的文件（大小写不敏感）。 | `exclude_video_codecs = ["hevc", "av1"]` |
| **排除音频编码** | 跳过包含指定音频编码的文件（大小写不敏感）。 | `exclude_audio_codecs = ["opus"]` |

> **性能提示**：仅使用文件名匹配、`file_mtime` 和 `size` 时，程序不会调用 ffprobe，扫描速度更快。使用上表中后 8 项任何条件都会触发 ffprobe。
>
> **单位语义**：容量遵循 IEC 80000-13——`KB/MB/GB` 为十进制（1KB = 1000B），`KiB/MiB/GiB` 为二进制（1KiB = 1024B）；码率同样使用十进制词头（1Kbps = 1000bps）。

### 第七步：容错设置

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `failure_count` | 单个文件处理失败后的最大容忍次数，超过后永久跳过该文件（成功一次即清零）。 | `3` |
| `fallback_count` | 连续 FFmpeg 失败多少次后切换备用命令。仅当配置了 `ffmpeg_cmd_fallback` 时需要设置。 | `0`（不启用） |
| `ffmpeg_timeout` | 单文件处理超时（秒），超时终止 FFmpeg 进程并按失败计数。 | `0`（不限） |

```toml
failure_count = 3

ffmpeg_cmd_fallback = """
ffmpeg -y \
  -i "{input}" \
  -c:v libx264 \
  -preset medium \
  -crf 28 \
  "{output}-encoded.mp4"
"""
fallback_count = 3   # 主命令连续失败 3 次后启用备用命令（此后持续使用备用命令）
```

### 第八步：多任务与依赖（可选）

**你需要同时监控多个不同的目录并执行不同的处理吗？**

- **不需要** → 配置一组 `[[tasks]]` 即可，配置完成。
- **需要** → 在配置文件中添加多个 `[[tasks]]` 节，每个任务独立重复第三步到第七步：

```toml
# 第一组任务
[[tasks]]
name = "视频转码"
source_dir = "./videos"
dest_dir = "./output"
ffmpeg_cmd = '...'
[tasks.filter]
include_patterns = ["*.mp4", "*.mkv"]

# 第二组任务
[[tasks]]
name = "音频处理"
source_dir = "./music"
dest_dir = "./audio"
ffmpeg_cmd = '...'
depends_on = ["video"]        # 【选填】等 "video" 任务本轮完成后才执行本任务
[tasks.filter]
include_patterns = ["*.flac", "*.wav"]
```

> 每个 `[[tasks]]` 节需紧邻自己的 `[tasks.filter]` 节（如果存在）。任务间 `source_dir` 不得相同或嵌套——重叠目录的状态会互相干扰，加载期会直接拒绝。

---

将以上各步骤中选择的配置项合并，即得到完整的 `config.toml`。完整结构可参考 `config/config.example.toml`。

## 🐳 Docker 部署说明

- 镜像基于 `python:3.14-slim`，内置静态构建的 FFmpeg（n8.1）与 VAAPI/NVIDIA 驱动；
- Intel / AMD GPU 透传：`devices: ["/dev/dri:/dev/dri"]`；NVIDIA GPU 参见 `docker-compose.yml` 注释段；
- 挂载目录：`config`（配置）、`logs`（日志）、`data`（状态库）、`source` / `dest` / `backup`（业务目录）；
- 容器内工作目录为 `/app`，所有相对路径配置均以该目录为基准。

## 🧰 可执行文件（Windows / macOS）

```bash
uv run --group build python scripts/build_executable.py    # 构建当前平台的 onedir 发行包
```

产物为 `dist/MediaConveyor-<版本>-<平台>.zip`；可执行文件**不内置 FFmpeg**，运行要求系统中已安装并在 PATH 中。

## 📄 许可证

[MIT](./LICENSE)
