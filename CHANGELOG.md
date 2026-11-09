# 更新日志

---

## 未发布

---

## [0.6.1] - 2026-11-09

由于底层架构完全重写，可能有部分行为变更未在下文中列出。

### 破坏性变更

- 配置文件格式与 V1 完全不兼容，需按文末迁移指南改写：`[global]` 全局节取消，全局配置项上移至顶层；`scan_interval` 由 `[schedule]` 调度节取代（省略该节 = 一次性运行；新增 cron 模式）。
- 任务新增必填项 `output_filename`（输出文件名模板）；`ffmpeg_cmd` / `ffmpeg_cmd_fallback` 改为纯参数模板——ffmpeg 可执行文件与预置参数（覆盖输出 `-y`、禁用 stdin `-nostdin` 等）由程序注入，模板中只填写 FFmpeg 参数；`{output}` 改为完整输出路径，占位符（`{input}` / `{output}`）由程序按平台自动加引号，不再手写引号。
- `log_dir` 与新增的 `data_dir`（默认 `./data`）的相对路径改以程序所在目录为基准解析，不再跟随工作目录。
- 状态库更换为 `data/` 目录下的 `state.db`（沿用 SQLite，表结构新增任务隔离键与源文件 size/mtime 快照）：旧库不迁移，首次启动视为全新状态。
- 过滤条件字段名与 V1 保持一致；范围型条件只接受 `{ min = ..., max = ... }` 字典写法，非法值不再静默按 0 处理，改为加载期显式报错。
- 容量与码率单位语义改为 IEC 80000-13（1KB=1000B、1KiB=1024B；码率十进制 1K=1000bps）。

### 新增

- 基于骨架获得的实例控制能力：`start`（分离式后台实例）/ `log`（滚动显示日志）/ `stop`（三级停止：默认完成当前文件、`--fast` 立即中止、`--slow` 完成本轮）/ `run-once`（让休眠中的实例立即执行一轮）子命令。
- 任务依赖：`depends_on` 声明前置任务，前置任务本轮完成后才执行（配置顺序即执行顺序）。
- 配置热重载：运行中修改 `config.toml` 自动生效，加载失败回退备份配置。
- `ffmpeg_timeout` 配置项：单文件转码超时（秒），超时终止 FFmpeg 进程并按失败计数。
- 多任务重叠源目录的状态隔离：状态记录以任务身份为键，互不串扰。
- 日志级别新增 `SUCCESS`（处理成功事件专用），`log_level` 可选值随之扩展。
- 源文件成功处理后被替换为新内容时，记录成功时的源文件大小和修改时间，内容变化后会重新处理。
- 为 Windows 平台适配了实例锁。
- FFmpeg 输出先写入目标目录下的临时文件（`.mc-ffmpeg-part.` 前缀）：处理中断不再遗留不完整的目标文件；保留名文件被过滤流水线无条件跳过。
- 打包版可执行文件，覆盖 Windows/macOS × AMD64/ARM64，内置 FFmpeg，无需另外安装。
- `failure_dir` 失败隔离目录：文件失败次数达到 `failure_count` 上限后移入该目录（保持子目录结构），不再留在源目录。
- 将裸 FFmpeg 状态行升级为全新的中文状态行，并支持包括预估时间在内的更多数据。
- 支持更多视频类型显示媒体时长。
- 任务级 `process_order` 处理顺序配置项：待处理文件可选择以路径名称、体积、时间新旧为顺序来处理。

### 修复

- 备份目录移动失败此前会中断整个监听循环，现按单文件失败计数兜底。
- `failure_count = 0` 时的语义偏差。现在该配置表示失败文件每轮重试，永不跳过。

### 变更

- 优化了 ffprobe 的调用流程。
- 将 FFmpeg 进度输出的刷新频率从 5 秒拉长至 30 秒，并统一 TTY、非 TTY、日志文件三处的表现形式。
- 状态记录仅在义务存续期间保留：源文件离开源树（删除 / 备份 / 直传移走）后不留记录，重新放回的同名文件从头处理。
- 媒体时长的显示时机从开始处理时移至正在处理时。

### 迁移指南

以下对照以 V1 配置为基准，逐项改写后即可在 V2 使用。

#### 1. 全局配置移出 `[global]` 节，`scan_interval` 改为 `[schedule]` 节

V2 没有 `[global]` 节：`log_dir` / `max_log_files` / `log_level` 直接写在配置文件顶层（另有新增的 `instance_name` 实例标识名与 `data_dir` 状态库目录）。遗留的 `[global]` 节会被整体忽略，其中的配置不再生效。

旧配置：

```toml
[global]
scan_interval = 60
log_dir = "logs"
max_log_files = 7
log_level = "INFO"
```

改为：

```toml
log_dir = "./logs"
max_log_files = 7
log_level = "INFO"

[schedule]
mode = "interval"
interval_seconds = 60
```

- 原 `scan_interval` 的秒数填入 `[schedule]` 的 `interval_seconds`（`mode = "interval"`）；原 `scan_interval = 0`（一次性运行）对应**省略整个 `[schedule]` 节**。
- 新增 `mode = "cron"` 定时模式（5 段 `cron_expr`）与 `first_run_delay` 首轮延迟（默认 -1，等待至下个调度时间点）。
- 相对路径基准变更：`log_dir` / `data_dir` 以程序所在目录为基准解析，不再跟随启动时的工作目录；依赖工作目录的使用场景请改为绝对路径。

#### 2. 任务新增必填项 `output_filename`，`ffmpeg_cmd` 占位符改写

每个任务必须配置 `output_filename`（输出文件名模板）：`{basename}` 占位源文件名（不含扩展名），输出扩展名直接写在模板里，且须为纯文件名（不允许路径分隔符）。

命令模板的三处配套变化：

- 模板只写 FFmpeg 参数：ffmpeg 可执行文件与预置参数（覆盖输出 `-y`、禁用 stdin `-nostdin` 等）由程序自动注入，模板中开头的 `ffmpeg` 与 `-y` 需删除；
- `{output}` 由「目标目录下不含扩展名的基础文件名」改为「完整输出路径」，扩展名由 `output_filename` 决定，不再在命令里拼接；
- 占位符由程序按平台自动加引号，模板中不再手写引号（`{input}` 同理）。

`ffmpeg_cmd_fallback` 与主命令共用同一个 `output_filename`，作同样改写。未配置 `output_filename` 的任务在加载期报错并被跳过。

旧配置：

```toml
ffmpeg_cmd = """
ffmpeg -y \
  -i "{input}" \
  -c:v libx264 \
  -preset fast \
  -crf 23 \
  "{output}-encoded.mp4"
"""
```

改为：

```toml
output_filename = "{basename}-encoded.mp4"

ffmpeg_cmd = """
-i {input} \
  -c:v libx264 \
  -preset fast \
  -crf 23 \
  {output}
"""
```

#### 3. 状态库不迁移

状态库由 `data/media-conveyor.db` 更换为 `data/state.db`（目录由 `data_dir` 指定，默认 `./data`）。旧库不读取、不迁移：首次启动视为全新状态，源目录中已处理过的文件会被再次处理。升级前如不希望重复转码，请先移走源目录中的文件，或接受一轮重复处理。

新库以任务身份为键隔离记录，并保存源文件 size/mtime 快照：处理成功后被替换为新内容的源文件会自动重新处理。

#### 4. 过滤条件的单位语义与非法值校验

过滤字段名与 V1 一致，无需迁移；两处行为变化需要留意：

- 容量与码率单位遵循 IEC 80000-13。容量 `KB/MB/GB/TB` 按十进制（与 V1 相同，无需改写），另新增 `KiB/MiB/GiB/TiB` 二进制单位；码率统一按十进制（1K = 1000 bps），V1 按 1024 进制解析，升级后同样的字符串实际阈值略有缩小（如 `10M`：10 485 760 → 10 000 000 bps），需要维持原阈值的请按十进制重新折算（如直接填裸数字 `10485760`，单位为 bps）。
- 范围条件只接受 `{ min = ..., max = ... }` 字典写法，非法值（无法解析的单位、类型误写等）在加载期显式报错并跳过任务，不再静默按 0 处理；升级后如出现相关报错日志，修正对应配置值即可。

---

[0.6.1]: https://github.com/HexStan/MediaConveyor/releases/tag/v0.6.1
