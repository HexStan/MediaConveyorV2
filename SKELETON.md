# SKELETON.md —— 骨架归属划分

## 定位

- 本项目由两部分组成：上游 SmartArchiver 的常驻任务骨架（调度、实例控制、工程化设施），与本项目自实现的媒体处理领域功能（目录监听 + FFmpeg 处理，领域实现源自 MediaConveyor V1）。
- 本文档以粗粒度划分骨架与领域自实现的边界。划分以机制归属为准，不追代码逐行一致；领域字段级迭代不应改变归属结论，本文档无需随之更新。
- 上游仓库位于本地同级目录 `../SmartArchiver`。改动骨架部分的机制语义前，须对照上游确认。

## 归属总览

| 归属 | 范围 | 说明 |
|---|---|---|
| 骨架 | `main.py` | CLI 解析与分发 |
| 骨架 | `src/app/` | 常驻调度循环与任务依赖编排（runtime）、start/log/stop/run-once 控制子命令（control）、单实例锁与跨进程控制通道（instance）、三级停止信号（lifecycle）、配置加载与热重载（config）、全局上下文（context）、运行时路径（paths） |
| 骨架 | `src/fs/` | 本地文件 I/O 原语：原子写入、传输中止注入、扫描枚举、传输与删除、空目录清理 |
| 骨架 | `src/output/` | 日志记录与轮转（logger）、日志跟随器（logtail）、终端状态栏（statusbar）、统一表现层（presentation）、FFmpeg 进度多路输出（progress） |
| 骨架 | `src/platform_info.py` | 平台判定唯一入口 |
| 骨架 + 领域适配 | `src/core/task_config.py` / `task_plan.py` / `types.py` | “TOML 配置 → 类型化任务 → 加载期静态校验 → 依赖编排”的解析框架与任务结果枚举来自骨架；任务字段、文件动作枚举值为领域定义 |
| 骨架 + 领域适配 | `src/core/handlers/base.py` / `__init__.py` | 处理器基类与装配入口的模式来自骨架；模板方法编排细节为本项目实现 |
| 骨架 + 领域适配 | `src/validation.py` | 共享校验基建的定位来自骨架；校验对象为本项目的标识符体系 |
| 领域自实现 | `src/core/state.py` | SQLite 状态库：防重复处理、失败计数、fallback 切换的唯一依据 |
| 领域自实现 | `src/core/media/` | 媒体领域：配置值解析、ffprobe 探测、过滤流水线、FFmpeg 命令模板填充与执行 |
| 领域自实现 | `src/core/handlers/ffmpeg_task.py` | FFmpeg 任务处理器：目录监听流水线与源文件善后 |

tests/ 中对应上述各层的测试文件遵循同样的归属划分（app / fs / output 层测试对应骨架设施，media / state 层测试对应领域实现）。

## 相对上游裁剪的能力

以下能力存在于上游 SmartArchiver，本项目未纳入；领域需求恰好命中时，可回上游取材移植：

- 远端能力：`src/remote/`（HTTP 远端客户端与服务端）、`src/ssh/`（SSH 远端）。
- 规则引擎与目标寻址：`src/core/dest.py`、`filters.py`、`patterns.py`。
- 任务模式处理器：`src/core/handlers/standard.py`（move/copy）、`rotate.py`（限额轮转）、`sync.py`（镜像同步）。
- 辅助设施：`src/core/registry.py`（处理器注册中心）、`src/core/history.py`（失败历史文件）。

## 演化规则

- 允许为领域需求修改骨架代码，但调度循环、单实例控制、停止信号、配置热重载、任务依赖编排等机制语义不得偏离上游；语义级改动前须对照上游 `../SmartArchiver` 确认实现方式。
- “骨架 + 领域适配”范围内的改动以领域字段与领域编排为界；骨架机制的结构（模块职责、依赖方向）保持与上游概念对应。
- 领域自实现部分不受上游约束，按 AGENTS.md 的代码规范性要求演进。
