# 更新日志

---

## 0.1.0-dev

### 破坏性变更

- 项目基于 SmartArchiver 的常驻任务骨架全面重构，配置文件格式与 1.x 完全不兼容：
  任务定义迁移至 `[[tasks]]` + `mode = "transcode"`，过滤条件迁移至 `[tasks.filter]`
  （原 `[tasks.filter]` 字段名保持一致，新增范围型媒体属性条件的加载期校验）。
- 全局节变更：`scan_interval` 由 `[schedule]` 调度节取代（省略该节 = 一次性运行；
  `interval_seconds` 等价原 `scan_interval`；新增 cron 模式）。
- 状态库文件位置变更：`data/` 目录下为 `state.db`（沿用 SQLite，表结构新增任务隔离键
  与源文件 size/mtime 快照；旧库不迁移，首次启动视为全新状态）。
- 新增 `data_dir` 配置项（默认 `./data`）；`log_dir` / `data_dir` 相对路径改以程序所在
  目录为基准解析，不再跟随工作目录。

### 新增

- 基于骨架获得的实例控制能力：`start`（分离式后台实例）/ `log`（滚动显示日志）/
  `stop`（三级停止：默认完成当前文件、`--fast` 立即中止、`--slow` 完成本轮）/
  `run-once`（让休眠中的实例立即执行一轮）子命令；单实例锁在 Windows 上同样生效。
- 调度能力：`[schedule]` 支持 `interval` 与 `cron` 两种模式及 `first_run_delay` 首轮延迟。
- 任务依赖：`depends_on` 声明前置任务，前置任务本轮完成后才执行（配置顺序即执行顺序）。
- 配置热重载：运行中修改 `config.toml` 自动生效，加载失败回退备份配置。
- `ffmpeg_timeout` 配置项：单文件转码超时（秒），超时终止 FFmpeg 进程并按失败计数。
- 多任务重叠源目录的状态隔离：状态记录以任务身份为键，互不串扰。

### 修复

- 源文件成功处理后被替换为新内容时，此前永不重新处理；现记录成功时的源文件
  size/mtime，内容变化后自动重新转码。
- FFmpeg 子进程不再失控：登记到生命周期协调器，`stop --fast` 时整树终止，
  不再遗留孤儿进程；stdout 管道改为丢弃，消除潜在的无输出死锁。
- `stable_duration` 的等待不再阻塞整个监听循环：睡眠分片化，可被停止指令立即打断。
- 过滤条件只认 `size = { min = ..., max = ... }` 字典写法，字符串误写此前被静默当作 0；
  现在所有范围条件在加载期解析，非法值显式报错。
- ffprobe 失败信息此前仅 print 到控制台、不进日志文件；现统一经日志系统输出。
- 消除过滤未触发 ffprobe 时的二次 ffprobe 调用（日志展示不再重复探测）。
- 备份目录移动失败此前会中断整个监听循环；现按单文件失败计数兜底。
- 容量与码率单位语义统一遵循 IEC 80000-13（1KB=1000B、1KiB=1024B；码率十进制
  1K=1000bps），修复原码率解析 1024 进制与容量 1000 进制不一致的问题。
- Windows 单实例锁真正生效（msvcrt 实现）；进程树终止前校验可执行文件名，防 PID 复用误杀。

### 变更

- 解析器依赖由 humanfriendly 迁移至 humanize（展示）与 pydantic ByteSize（容量）。
- FFmpeg 进度输出经日志系统 5 秒节流记录，进入日志文件（原仅控制台单行覆盖打印）。
- PyInstaller 可执行发行包不再内置第三方工具，运行要求 PATH 中已安装 FFmpeg；
  Docker 镜像内置静态构建的 FFmpeg 与 VAAPI/NVIDIA 驱动，行为不变。

---

# 历史版本（MediaConveyor 1.x）

> 以下为重构前 1.x 系列的原始变更日志，供追溯。

---

---

## 未发布

### 新增

- 基于 fnmatch 模式的文件过滤流水线（`[tasks.filter]`）：
  - `include_patterns`：指定要识别的文件，不填或为空则包含所有文件。
  - `exclude_patterns`：匹配到的文件将被跳过。
  - `passthrough_patterns`：匹配到的文件不经过 FFmpeg，直接移动到目标目录（仍受 `file_mtime` 约束）。
  - 三者均以 fnmatch 语法匹配相对于 `source_dir` 的文件路径（不区分大小写，路径分隔符统一为 `/`），按 include → exclude → passthrough 的顺序过滤，支持按目录、路径关键字等更灵活的匹配方式。

### 变更

- 状态存储从 JSON 文件迁移至 SQLite 数据库（`data/` 目录），提升数据一致性与查询效率。
- 未配置任何文件名过滤条件时，程序默认处理源目录中的所有文件（此前默认仅处理 `.mp4` 文件）。

### 移除

- 配置项 `input_formats` 和 `direct_move_formats` 已移除，由 `include_patterns` 和 `passthrough_patterns` 取代。

### 破坏性变更

- 状态文件路径变更：`logs/state.json` → `data/` 目录下的 SQLite 数据库。
- Docker 用户需在 `docker-compose.yml` 中新增 `./data:/app/data` 卷挂载，以确保数据库文件持久化。
- 文件过滤配置从扩展名列表（`input_formats`/`direct_move_formats`）改为 fnmatch 模式列表（`include_patterns`/`exclude_patterns`/`passthrough_patterns`）。旧配置项不会自动迁移，需手动转换（见迁移指南）。

### 迁移指南（v4.x -> 未发布）

#### 1. 状态存储迁移

本版本不再提供自动数据迁移。v4.x 及更早版本的 JSON 状态数据（`logs/state.json`）不会迁移至 SQLite，首次启动时视为全新状态，所有文件将被重新扫描，此前的失败计数与处理成功时间记录将丢失。

#### 2. 文件过滤配置迁移

旧配置不会自动转换，请参考以下对照关系手动修改：

```toml
# v4.0.0
[tasks.filter]
input_formats = ["mp4", "mkv"]
direct_move_formats = ["srt"]

# 手动转换后
[tasks.filter]
include_patterns = ["*.mp4", "*.mkv"]
exclude_patterns = []
passthrough_patterns = ["*.srt"]
```

**注意默认行为变化**：如果旧配置未显式设置 `input_formats`（旧版默认仅处理 `.mp4` 文件），转换后 `include_patterns` 为空，程序将处理源目录中的**所有文件**。如需保持原行为，请手动添加：

```toml
[tasks.filter]
include_patterns = ["*.mp4"]
```

#### 3. Docker 用户

需在 `docker-compose.yml` 中新增 `data` 卷挂载：

```yaml
volumes:
  - ./config:/app/config
  - ./data:/app/data      # 新增此行
  - ./logs:/app/logs
```

---

## 旧版本号体系（v1.0.0 – v4.0.0）

> **注**：项目版本号已于 2026-10 重置，自 `0.7.0-dev` 起采用新的版本号体系重新开始迭代。以下 v1.0.0 至 v4.0.0 为旧版本号体系下的历史记录，与当前版本体系不连续；对应的 Tag 与 GitHub Release 已一并清理归档。

---

## [4.0.0] - 2026-06-26

### 新增

- `backup_dir` 现为可选项。当留空且 `remove_source = false` 时，源文件处理完成后保留在原位置不做移动。

### 移除

- **命令行参数**：`-c` / `--config`、`--log-dir`、`--state-file` 已移除。配置文件路径固定为 `config/config.toml`，状态文件路径固定为 `logs/state.json`。
- **环境变量覆盖**：不再支持通过以下环境变量覆盖配置项：
  - `VIDEOWATCHDOG_SCAN_INTERVAL`
  - `VIDEOWATCHDOG_LOG_DIR`
  - `VIDEOWATCHDOG_MAX_LOG_FILES`
  - `VIDEOWATCHDOG_LOG_LEVEL`
  - `VIDEOWATCHDOG_SOURCE_DIR`
  - `VIDEOWATCHDOG_DEST_DIR`
  - `VIDEOWATCHDOG_BACKUP_DIR`
  所有配置现仅通过 `config/config.toml` 设置。

### 破坏性变更

- 命令行参数完全移除。此前通过 `-c`、`--log-dir`、`--state-file` 传参的启动方式不再有效。
- 环境变量覆盖完全移除。此前通过 `VIDEOWATCHDOG_*` 环境变量覆盖配置的方式不再有效。
- 状态文件内部 JSON 结构变更（扁平结构 → 分类结构）。v3.x 格式的状态文件在首次启动时视为空，此前的失败计数与处理成功时间记录将丢失。

### 迁移指南（v3.x -> v4.0.0）

#### 1. CLI 参数移除

如果之前通过命令行参数指定路径，请改为在 `config/config.toml` 中配置：

```
# v3.x 使用方式（不再可用）
python main.py -c /path/to/config.toml --log-dir /var/log --state-file /var/state.json

# v4.0.0 使用方式
python main.py
# 配置文件固定为 config/config.toml
# 日志目录在 [global] 中设置 log_dir
# 状态文件固定为 logs/state.json
```

#### 2. 环境变量覆盖移除

如果之前在 Docker 或 systemd 等环境中通过环境变量覆盖配置：

```yaml
# v3.x (docker-compose.yml)
environment:
  - VIDEOWATCHDOG_LOG_LEVEL=DEBUG
  - VIDEOWATCHDOG_SOURCE_DIR=/custom/source
```

请改为直接在 `config/config.toml` 中设置对应项：

```toml
# config/config.toml
[global]
log_level = "DEBUG"

[[tasks]]
source_dir = "/custom/source"
```

#### 3. 状态文件格式变更

旧版状态文件 `logs/state.json` 的数据（失败计数、处理成功时间）不会迁移到新格式，首次启动相当于全新状态，所有文件将被重新扫描。如有依赖状态记录的上下游逻辑，需注意此行为变化。

---

## [3.0.0] - 2026-06-15

### 新增

- 可配置的日志等级（`log_level`），支持 `DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL`。设置为 `DEBUG` 时会输出详细的文件跳过原因。
- 文件过滤器（`[tasks.filter]`），支持按以下条件筛选文件：
  - 文件大小（`size`），支持 `"1MB"` 等可读格式。
  - 媒体时长（`duration`），支持 `"10s"`、`"30m"`、`"2h"` 等格式。
  - 视频/音频/总比特率（`video_bitrate`、`audio_bitrate`、`total_bitrate`），支持 `"100K"`、`"10M"`、`"1MBPS"` 等格式。
  - 帧率（`framerate`）。
  - 视频短边分辨率（`short_side`）。
  - 按编码器排除（`exclude_video_codecs`、`exclude_audio_codecs`），如 `["hevc", "av1"]`。
- 直接移动格式（`direct_move_formats`），指定后缀的文件会跳过 FFmpeg 处理，直接从源目录移动到目标目录。
- 源文件删除选项（`remove_source`），设置为 `true` 后处理成功直接删除源文件，不再保留备份。
- 延迟删除选项（`source_expired_minutes`），配合 `remove_source` 使用，处理完成后保留源文件指定分钟数再删除。
- 所有日志消息添加任务名称前缀（如 `【Task 1】`），便于多任务场景下区分。
- 命令行参数支持：
  - `-c` / `--config`：指定配置文件路径。
  - `--log-dir`：覆盖日志目录。
  - `--state-file`：覆盖状态文件路径。
- 环境变量覆盖支持：
  - `VIDEOWATCHDOG_SCAN_INTERVAL`、`VIDEOWATCHDOG_LOG_DIR`、`VIDEOWATCHDOG_MAX_LOG_FILES`、`VIDEOWATCHDOG_LOG_LEVEL`。
  - `VIDEOWATCHDOG_SOURCE_DIR`、`VIDEOWATCHDOG_DEST_DIR`、`VIDEOWATCHDOG_BACKUP_DIR`。
- Docker 多架构支持：通过 BtbN 静态构建分别支持 amd64 和 arm64。
- 智能 ffprobe 优化：仅在过滤器需要媒体元数据时才调用 ffprobe，无需时长/比特率/编码器等条件时跳过，提升性能。

### 变更

- 扫描间隔改为全局参数，所有任务共享同一个 `scan_interval` 计时周期。
- 术语更新：日志和文档中将"转码"改为"处理"，"监控"改为"监听"。
- Docker 镜像中的 FFmpeg 安装方式从 apt 改为下载 BtbN 静态构建。
- Docker Compose 默认挂载路径从 `./input`/`./output`/`./processed` 改为 `./source`/`./dest`/`./backup`。
- 状态文件内部格式变更，旧格式的状态文件首次启动时将被视为空。

### 修复

- 修复直接移动文件时仍进行媒体信息检查的问题。
- 修复文档中 FFmpeg 的拼写错误。

### 移除

- 移除 `suffix` 和 `output_format` 配置项，用户需通过 `ffmpeg_cmd` 中的 `{output}` 占位符自行构造完整输出文件名。
- 移除 Docker Compose 中的 `restart: unless-stopped`，容器不再默认自动重启。

### 破坏性变更

配置文件结构发生重大变化，v2.x 的配置文件无法直接使用。

### 迁移指南（v2.x -> v3.0.0）

| 旧配置键（v2.x） | 新配置键（v3.0.0） | 说明 |
|---|---|---|
| `[[tasks]].monitor_dir` | `[[tasks]].source_dir` | 重命名 |
| `[[tasks]].output_dir` | `[[tasks]].dest_dir` | 重命名 |
| `[[tasks]].processed_dir` | `[[tasks]].backup_dir` | 重命名；当 `remove_source = true` 时可不填 |
| `[[tasks]].scan_interval` | `[global].scan_interval` | 移至全局配置 |
| `[[tasks]].input_formats` | `[tasks.filter].input_formats` | 移入 filter 子表 |
| `[[tasks]].file_mtime` | `[tasks.filter].file_mtime` | 移入 filter 子表 |
| `[[tasks]].suffix` | *(已移除)* | 请在 `ffmpeg_cmd` 的 `{output}` 占位符中直接拼接 |
| `[[tasks]].output_format` | *(已移除)* | 请在 `ffmpeg_cmd` 的 `{output}` 占位符中直接拼接 |
| `{output}` 占位符行为 | `{output}` 占位符行为 | 旧版解析为完整路径（含扩展名）；新版解析为基础路径（不含扩展名），需自行追加后缀 |
| `config/config.toml.example` | `config/config.example.toml` | 示例文件重命名 |
| Docker 挂载 `./input`/`./output`/`./processed` | `./source`/`./dest`/`./backup` | 挂载路径重命名 |

迁移步骤：
1. 备份旧配置文件。
2. 参考 `config/config.example.toml` 创建新的 `config/config.toml`。
3. 将旧配置值逐一映射到新键。
4. 将 `{output}` 的用法从 `"{output}"` 改为 `"{output}-encoded.mp4"`（根据需求调整后缀）。
5. 如果使用 Docker，更新 `docker-compose.yml` 中的卷挂载路径。

---

## [2.0.2] - 2026-03-24

### 新增

- 运行时每日日志轮转：程序运行期间跨天时自动切换到新的日志文件。
- 运行时自动清理旧日志：跨天轮转时自动删除超过 `max_log_files` 数量的旧日志文件。此前清理仅在启动时执行。

### 变更

- `.dockerignore` 新增排除 `.github` 目录，减少 Docker 构建上下文大小。

---

## [2.0.1] - 2026-03-23

无用户可感知的变更，仅包含代码风格调整和 `.dockerignore` 文件新增。

---

## [2.0.0] - 2026-03-19

### 新增

- FFmpeg 备用命令支持：当 FFmpeg 命令连续失败达到 `fallback_count` 次后，自动切换到 `ffmpeg_cmd_fallback` 执行。可用于主命令失败时降级为更兼容的编码参数。

### 变更

- 配置文件和示例模板从项目根目录移至 `config/` 子目录。
- Docker Compose 挂载方式从单文件绑定改为整个 `config/` 目录挂载。
- 配置键 `max_retries` 重命名为 `failure_count`，功能不变。
- 示例配置中将第二个任务的 `processed_dir` 从 `./done2` 修正为 `./processed2`。

### 修复

- 移除了 FFmpeg 执行前冗余的目录创建操作，避免失败时产生空目录。
- `.gitignore` 新增 `logs/` 目录，防止日志文件被误提交。

### 破坏性变更

配置文件路径和键名发生变更，v1.x 的配置文件需要调整。

### 迁移指南（v1.x -> v2.0.0）

1. 在项目根目录创建 `config/` 文件夹。
2. 将原有的 `config.toml` 移动至 `config/config.toml`。
3. 将配置中的 `max_retries` 重命名为 `failure_count`。
4. 如果使用 Docker，确保 `docker-compose.yml` 中挂载的是 `./config:/app/config` 而非单文件。
5. （可选）添加 `fallback_count` 和 `ffmpeg_cmd_fallback` 以启用备用命令功能。

---

## [1.0.1] - 2026-03-19

### 新增

- 转码成功时日志中输出实际耗时信息。

### 变更

- 错误消息的详细信息现在换行显示，提升多行错误输出（如 FFmpeg 错误日志）的可读性。

### 修复

- 修复转码期间目标目录被外部删除后，移动源文件时程序崩溃的问题。现在会自动重建缺失的目录。

---

## [1.0.0] - 2026-03-17

- 首次发布 Realease。

[4.0.0]: https://github.com/HexStan/MediaConveyor/releases/tag/v4.0.0
[3.0.0]: https://github.com/HexStan/MediaConveyor/releases/tag/v3.0.0
[2.0.2]: https://github.com/HexStan/MediaConveyor/releases/tag/v2.0.2
[2.0.1]: https://github.com/HexStan/MediaConveyor/releases/tag/v2.0.1
[2.0.0]: https://github.com/HexStan/MediaConveyor/releases/tag/v2.0.0
[1.0.1]: https://github.com/HexStan/MediaConveyor/releases/tag/v1.0.1
[1.0.0]: https://github.com/HexStan/MediaConveyor/releases/tag/v1.0.0
