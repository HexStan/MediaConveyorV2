# 测试指南

本目录的测试体系遵循一个明确的分层策略：**灰盒测试为主体框架，白盒单元测试补外部不可见的盲区**。

## 为什么是“灰盒为主、白盒补盲”

MediaConveyor 的本质是一个“监听目录树 + 配置 → 新目录树 + SQLite 状态”的变换工具，它**会处理、移动和删除用户的文件**。因此：

1. **正确性有一个天然的外部判定标准**——最终哪些文件被处理、直传、删除、备份、保留在原地，以及状态库里的记录。断言真实文件系统与状态库的最终状态，比断言内部某个分支被走过信号强得多。
2. **大部分代码是 I/O 编排**（文件系统、ffprobe/ffmpeg subprocess）。对它们做白盒测试意味着大量 mock，测的其实是 mock 本身；FFmpeg 的处理语义、编解码器行为都在白盒边界之外。
3. **少数行为从外部根本观察不到**（命令模板填充、输出流解析、进度节流），只能靠白盒单元测试兜住。

## 分层结构

| 层 | 文件 | 被测对象 | 驱动方式 |
|----|------|---------|---------|
| 灰盒·模式处理器 | `test_greybox_ffmpeg_task.py` | FFmpegTaskHandler（扫描/过滤/稳定性/处理/善后/状态记录生命周期/待删除清理/多任务隔离） | `run_task()` 入口 + 临时目录树 + `stub_ffmpeg` 进程边界打桩 |
| 白盒·媒体属性探测 | `test_media_probe.py` | ffprobe 封装（JSON 解析、容错降级、首个流选取） | `subprocess.run` 进程边界打桩 |
| 白盒·配置值解析 | `test_media_parsing.py` | 容量/时长/码率解析器（IEC 80000-13 单位语义、显式报错） | 纯函数直接调用 |
| 白盒·文件过滤 | `test_media_filter.py` | classify 流水线、file_mtime 冷却、媒体属性匹配 | 直接调用 |
| 白盒·FFmpeg 执行 | `test_media_ffmpeg.py` | 命令模板填充、progress 协议块解析与进度行渲染、终写/收尾时序、超时与 fast 中止 | `stub_ffmpeg`（conftest） |
| 白盒·状态库 | `test_state.py` | SQLite 记录语义（任务隔离、成功快照、计数清零） | 真实 SQLite（tmp 目录） |
| 白盒·任务配置模型 | `test_task_config.py` | 任务模型字段级校验契约（必填/强转/范围值加载期解析/未知字段拒绝） | 直接调用配置模型 + format_validation_error |
| 白盒+灰盒·任务依赖 | `test_task_dependencies.py` | 加载期 id/depends_on/路径关系校验（resolve_tasks）+ run_tasks 依赖编排 | 直接调用 resolve_tasks + 同步驱动 run_tasks（真实文件系统） |
| 白盒·调度循环 | `test_runtime_schedule.py` | 首次执行语义（first_run_delay）+ run-once 唤醒语义 + [schedule] 配置模型（ScheduleConfig） | 线程驱动 _run_interval/_run_cron + 真实文件系统；模型校验直接调用 |
| 白盒·实例控制 | `test_instance_control.py` | 锁探测、控制文件协议、运行状态文件、进程树终止 | 真实锁原语 + 子进程边界 |
| 白盒·停止信号 | `test_sigterm_fast_stop.py` | SIGTERM → fast 停止的接线映射 | 同步调用已注册处理器 + POSIX 子进程真实信号 |
| 白盒·实例锁 | `test_instance_lock.py` | SingleInstance 跨进程互斥、instance_name 解析 | 子进程边界 + 纯函数 |
| 白盒·日志跟随 | `test_logtail.py` | LogFollower（尾部回读/增量/切换） | 真实日志文件 + 手动步进 |
| 白盒·状态栏 | `test_statusbar.py` | StatusBar（滚动区域/反色绘制/降级） | 注入输出流、尺寸查询与 VT 开关 |
| 白盒·run-once 前置判定 | `test_control_run_once.py` | 子命令预检（存活 + 休眠期）与受理回执等待 | 真实锁原语 + 真实状态文件 |
| 白盒·quiet 确认返回 | `test_control_quiet.py` | `start --quiet` 启动确认等待与 `run-once --quiet` 的受理/未受理返回分支 | 真实锁原语 + 真实状态文件；`_spawn_detached` 进程边界打桩 |
| 白盒·文件系统原语 | `test_fs_scan.py` / `test_fs_abort.py` / `test_atomic_write.py` | scan 查询、chunk 边界中止、原子写入 | 真实文件系统 + 中止钩子注入 |
| 白盒·路径解析 | `test_app_paths.py` | 应用根目录锚定（仓库根 vs 可执行文件目录）+ 内置 FFmpeg 解析与 PATH 前置 | 纯函数（冻结标记 monkeypatch 注入） |
| 端到端·打包产物 | `test_e2e_packaged.py` | PyInstaller 产物（main 入口） | 子进程驱动；`MEDIACONVEYOR_BIN` 未设置时整体跳过 |

## 编写原则

**原则一：灰盒优先——能从入口测的，不从内部测。**
新测试先问：“能不能通过 `run_task()`（即 handler 的 `execute()` 入口）加一个临时目录树来驱动？”能，就写成灰盒测试：输入 = 目录树 + 任务配置，断言 = 输出目录树 + 状态库记录 + 日志。被测代码内部**不打桩**——Logger 用捕获型替身，StateManager 与文件系统用真的，FFmpeg 在进程边界打桩。任务的配置校验失败、源目录不存在等错误路径，也在这里以“错误日志 + 目录树无变化”的形式验证，不单独拆成校验函数的单测。

**原则二：白盒只补外部不可见的契约。**
只有当行为从 `execute()` 入口无法观察（或观察成本畸高）时，才写白盒单元测试。当前属于这一类的契约：
- **ffprobe 输出解析**：JSON 字段到媒体属性字典的映射、异常降级为全零默认值（`test_media_probe.py`）；
- **命令模板填充**：`{input}`/`{output}` 值按运行平台自动加引号、其他花括号显式报错、多行合并，`output_filename` 模板仅支持 `{basename}`（`test_media_ffmpeg.py`）；
- **输出流解析与进度消费**：stdout progress 块按 `progress=` 行切块与解析、中文进度行渲染、进程结束的 finish/close 时序（`test_media_ffmpeg.py`）；
- **进度多路输出**：日志文件定宽原位覆盖（文件长度不回缩、他线日志追加让位重锚、轮转重锚）、TTY/非 TTY 落笔节奏与终写、日志 emit 清行协调（`test_output_progress.py`，注入时钟）；
- **状态库记录语义**：成功快照（size/mtime）与失败计数是"重处理判定"的唯一依据，任务键隔离需按 (task_key, filepath) 精确钉死（`test_state.py`）；
- **单位语义**：容量 KB=1000/KiB=1024、码率十进制词头，无效配置显式报错（`test_media_parsing.py`）。
不在此列的内部细节（比如某个私有方法的返回结构）不要测——那会把测试焊死在实现上。

**原则三：打桩只发生在外部边界。**
桩只能放在**进程或网络边界**（`subprocess.Popen`、`subprocess.run`），因为这些是“不在我们控制之内的外部世界”。业务函数之间不许互相打桩——那会掩盖真实的集成缺陷。conftest 的 `stub_ffmpeg` fixture 是范式：拦截 `Popen` 记录构造出的命令行，断言我们传给 FFmpeg 的参数正确，而不假装自己测试了 FFmpeg 处理。

**原则四：确定性——禁止依赖真实时钟。**
- 所有 `now` 通过 `run_task(task, now=NOW)` 显式注入；
- 所有文件 mtime 在 `make_tree()` 里显式设定（`NOW`、`OLD`、`RECENT` 常量，或逐文件元组）；
- 不 sleep、不依赖“现在几点”、不依赖文件系统时间戳默认值（稳定性检查与调度唤醒测试中的短 sleep 仅用于等待确定性事件，不构成时序断言）。

**原则五：断言优先落在目录树快照上。**
`snapshot()` 返回 `{相对路径: 大小}`，是主断言对象；`handler.stats`、状态库查询和捕获的日志只作补充信号（如 `failed == 1`、“命令模板无效”日志）。文件内容需要区分时，单独读文件断言。

**原则六：一个测试只陈述一个行为。**
测试名就是行为描述（`test_exclude_overrides_passthrough`），读测试名应当能看懂它守护的契约。组合行为的验证交给灰盒场景测试，白盒测试保持单点。

**原则七：真实组件优先于替身。**
优先级：真实组件 > 捕获型替身（记录调用但不改变行为）> 行为型 mock（伪造返回值）。Logger 用捕获型（不改行为只记录）；StateManager、文件系统全部用真的。每引入一个行为型 mock，都要能回答“我在替谁做决定，依据是什么”。

## 基础设施

**`conftest.py`**
- `app_context`：初始化全局 AppContext——捕获型 `CaptureLogger` + **真实** `StateManager`（指向 tmp 目录）+ 默认 `GlobalConfig`；测试结束清理单例与处理器类级去重状态。
- `stub_ffmpeg`：进程边界打桩（记录命令行 `calls` + 可配置行为 `state`：returncode / output（stderr 错误输出）/ progress（stdout progress 块）/ hangs / on_start；成功退出且未配置 `on_start` 时，自动向命令行末尾的 part 路径写出产物，模拟 ffmpeg 的产出）。
- `run_task(task, now=None)`：灰盒测试统一入口。与生产装配路径对齐：任务字典先经加载期解析（`resolve_tasks`——任务模型校验 + 路径关系校验，错误日志在此给出，被拒绝的任务返回 None），再经 `create_handler` 构造处理器并执行，返回 handler 实例（供补充断言）。

**`helpers.py`**
- `NOW / OLD / RECENT`：确定性时间常量（`OLD` 距 `NOW` 30 天，`RECENT` 距 60 秒）。
- `make_tree(root, files, default_mtime=OLD)`：按 `{相对路径: bytes}` 声明式建树；值也可以是 `(bytes, mtime)` 元组单独指定时间。
- `snapshot(root)`：`{相对路径: 大小}` 快照，目录树断言的基准。

## 端到端：打包产物（条件启用）

`test_e2e_packaged.py` 以黑盒方式驱动 PyInstaller 打包产物，覆盖灰盒够不到的三段链路：

1. **TOML 配置形状 → 任务字典**：灰盒直接传入 Python 字典，绕过了 ConfigManager；
   端到端走真实的 config.toml 加载路径，钉死任务/过滤块在 TOML 中的写法；
2. **打包布局与入口启动**：冻结形态下应用根目录 = 可执行文件所在目录
   （`src/app/paths.py`），打包缺失模块、入口崩溃等只有真实产物能暴露；
3. **内置 FFmpeg 真实转码**：先以包内 `_internal/ffmpeg/ffmpeg` 合成测试视频
   （lavfi 源），再经程序注入的 `ffmpeg` 转码（命令组装见
   `src/core/media/ffmpeg.py`）——PATH 前置
   （`src/app/paths.py`）与内置二进制可执行性只有真实产物能验证。

约定：

- **启用条件**：环境变量 `MEDIACONVEYOR_BIN` 指向打包程序目录（如 `dist/MediaConveyor`）；
  未设置时整个模块跳过，日常源码开发不受影响。CI 中由 executable-build.yml 在构建后设置。
- **驱动方式**：把程序目录复制到 pytest 临时位置（不污染构建产物），写入测试 config.toml
  后以子进程运行二进制，断言退出码与目录树快照——驱动子进程即进程边界，符合原则三。
- **时钟说明**：二进制无法注入 now，仅依赖「真实当前时刻远超 `make_tree` 固定的过去
  时间戳 `OLD`」这一单调不变量；行为判定全部来自显式设定的 mtime，不 sleep。

本地运行：

```bash
python scripts/build_executable.py
MEDIACONVEYOR_BIN=dist/MediaConveyor python -m pytest tests/test_e2e_packaged.py -v
```

## 运行

```bash
python -m pytest            # 全部测试
python -m pytest tests/test_greybox_ffmpeg_task.py -v
```

## 新增测试的决策路径

1. 行为能从 `execute()` 入口观察？→ 写进 `test_greybox_ffmpeg_task.py`。
2. 不能观察，但属于对外契约（性能、顺序、单位语义）？→ 写进对应白盒文件，并在模块 docstring 里注明“为什么这里必须白盒”。
3. 涉及外部进程？→ 只在边界打桩，参考 conftest 的 `stub_ffmpeg`。
4. 三条都不满足 → 先怀疑这个测试有没有必要存在。
