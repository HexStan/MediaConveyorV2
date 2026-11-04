# AGENTS.md

MediaConveyor 是监听目录并调用 FFmpeg 处理音视频文件的自动化工具：源目录树 + 配置 → 目标目录树 + SQLite 状态库。仓库文档、用户可见的日志与错误信息一律使用中文；提交信息用 Conventional Commits + 中文描述（如 `fix: 修复过滤条件对空 patterns 的判定缺陷`）。

## 代码规范性要求

- 如果现有代码结构无法规范、优雅地实现维护者的需求：

  - 大胆增/删/改写模块，以保持低耦合、高内聚，避免出现补丁式代码。
  - 允许引入新库，不要为了“不增加依赖”而频繁自己造轮子。

- 如果：

  - 不确定某个功能应该使用何种方案来实现
  - 应该如何处理某种维护者未提及的情况
  - 维护者的需求描述或功能设计中有无法自洽的内容，或 Agent 认为有更好的方案
  - 有其他任何不确定的问题

  请向维护者询问。可在开发过程中多次询问，直至所有细节清晰。

## 文档编纂原则

- `AGENTS.md` 的定位是“规则条款和索引目录”，因此对增加内容要克制，确保仅包含非常必要的内容，所有细节由具体代码和其他文档来表达。
- 除了 `CHANGELOG.md`，所有文档和注释都不得出现迭代叙事文案，如“原来XXX，现在XXX”、“不再XXX”、“保留原有的XXX”等。
- 除非被明确要求，否则禁止修改 `ROADMAP.md`。
- 除非进入发布流程或被明确要求，否则禁止修改 `CHANGELOG.md`。

## 常用命令

```bash
uv sync --all-groups                                                    # 安装依赖到 .venv（严格按 uv.lock 锁定；dev 组 = 运行依赖 + ruff / pyrefly / pytest / 类型存根）
uv run python -m pytest                                                 # 全部测试（pyproject.toml 已设 pythonpath=. 和 testpaths=tests）
uv run python -m pytest tests/test_greybox_ffmpeg_task.py -v              # 单个测试文件
uv run ruff check .                                                     # Ruff 静态检查
uv run ruff format .                                                    # Ruff 格式化
uv run pyrefly check                                                    # Pyrefly 类型检查（strict 门禁，配置见 pyproject.toml 的 [tool.pyrefly]）
uv run python main.py                                                   # 前台运行，读取 config/config.toml，缺失即退出
uv run python main.py start                                             # 分离式后台实例（另有 log / stop / run-once）
uv run --group build python scripts/build_executable.py                 # 构建当前平台的可执行文件（PyInstaller onedir；构建时联网下载内置 FFmpeg，版本见 pyproject.toml 的 [tool.mediaconveyor]）
uv run python scripts/smoke_test.py "dist/MediaConveyor-*.zip"          # 对发行 zip 做冒烟检查（包结构 + 入口与内置 FFmpeg 可运行）
```

## 类型检查（权威配置：pyproject.toml 的 [tool.pyrefly]）

- 详见 `pyproject.toml` 中 `[tool.pyrefly]` 的注释。

## 架构速览

- 运行入口：`main.py` 仅 CLI 解析与分发；运行路径在 `src/app/runtime.py`，start/log/stop/run-once 子命令在 `src/app/control.py`。
- 加载期：`src/app/config.py` 的 `ConfigManager`（Pydantic）完成仅凭配置即可判定的校验；任务配置模型与过滤模型在 `src/core/task_config.py`，逐任务校验在 `src/core/task_plan.py` 的 `resolve_tasks()`。
- 运行期：处理器在 `src/core/handlers/`（基类 `BaseTaskHandler` 只做模板方法编排，`create_handler` / `process_task` 为装配入口）。
- 领域层：`src/core/media/` —— `parsing.py`（配置值解析）、`probe.py`（ffprobe 封装）、`filter.py`（文件过滤与媒体匹配）、`ffmpeg.py`（命令模板填充与执行）。
- 状态库：`src/core/state.py` 的 `StateManager`（SQLite）是防重复处理、失败计数与 fallback 切换的唯一依据。
- 底层：本地文件 I/O 原语在 `src/fs/`（仅标准库）；共享校验基建 `src/validation.py`（仅 pydantic）；共享平台探测 `src/platform_info.py`（唯一入口）。
- `AppContext`（`src/app/context.py`）是全局单例，logger / state 经 `AppContext.get()` 获取。
- 依赖方向必须无环：`app/` 可依赖全部；`core/` → `fs/`、`output/`、`core.media`；`output/` → `fs/`；领域包不得依赖 `app/` 装配模块，需要 app 层能力时由组合根注入（例外：`AppContext`、`app.paths` 等叶子助手；`TYPE_CHECKING` 下的类型级 import 不构成违规）。

## 上游骨架（SmartArchiver）

本项目由上游仓库 SmartArchiver（本地同级目录 `../SmartArchiver`）的常驻任务骨架裁剪而来，媒体领域功能自 MediaConveyor V1 移植；骨架逻辑与自实现逻辑的划分见 `SKELETON.md`。

- 骨架范围：`main.py`、`src/app/`、`src/core/handlers/__init__.py`、`src/core/handlers/base.py`、`src/core/task_config.py`、`src/core/task_plan.py`、`src/core/types.py`、`src/fs/`、`src/output/`、`src/platform_info.py`、`src/validation.py`。改动涉及上述路径前，先读取 `SKELETON.md` 确认归属；涉及机制语义时对照上游确认。
- 当开发要求与媒体处理领域无关、属于常驻任务工具的通用能力（调度语义、实例控制、停止信号、日志与表现设施、文件原语等）时，建议维护者先在上游 SmartArchiver 实现，再移植回本项目，避免骨架分叉。

## 测试约定（权威文档：tests/README.md）

详见 `tests/README.md`。

## 平台与版本约束

- Python 3.14+。CI 测试矩阵为 ubuntu + macos + windows × Python 3.14–3.15。
- 代码必须保持跨平台；Docker 运行环境基于 Debian，带 VAAPI/NVIDIA GPU 驱动与静态构建的 FFmpeg。

## 提交与发布（权威文档：RELEASE-GUIDE.md）

- 仅当维护者主动要求时执行对应流程，不得自行提交、推送或发布：
  - 要求提交变更 → 执行 RELEASE-GUIDE.md 的提交流程（前置：仓库存在未提交的变更）。
  - 要求发布版本 → 执行 RELEASE-GUIDE.md 的发布流程（前置：`pyproject.toml` 的 version 字段存在未提交的变更）。
