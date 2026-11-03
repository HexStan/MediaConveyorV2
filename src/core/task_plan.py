"""加载期任务解析：模型校验与 id/depends_on 静态校验。

职责边界：本模块在配置加载期完成所有"仅凭配置文件即可判定"的任务级
校验，产出与 tasks 列表索引对齐的 TaskSpec（config 为 None 表示被拒绝
的任务）；"运行期"的依赖满足判定（前置任务是否进入已完成集合）由
app.runtime 的任务循环完成。逐任务配置错误沿用项目惯例：错误日志 +
跳过该任务（blocked），不致命。

id 规则：
- 选填；合法字符集为 a-z、A-Z、0-9、-、_；
- 非法（类型错误/字符不符）视为无 id：任务照常执行，不可被依赖——
  降级而非拒绝，故不走模型校验，在本模块归一；
- 与前方任务重复时报错并跳过该任务——若静默降级为"无 id 照常跑"，
  后置任务的 depends_on 会解析到第一个同名任务，用户意图被篡改；
  跳过重复者让"先出现者获得 id"的解析结果无歧义。

depends_on 规则：
- 选填；字符串数组，单个字符串容错视为单元素列表；
- 仅允许引用配置顺序中更早任务的 id；引用不存在、自身或后置任务均
  报错并跳过该任务（配置顺序即执行顺序，依赖只是门禁）。
"""

import os
from typing import TYPE_CHECKING, Any, NamedTuple

from pydantic import ValidationError

from src.core.task_config import TaskConfig
from src.validation import ALIAS_PATTERN, format_validation_error

if TYPE_CHECKING:
    from src.output.logger import Logger


class TaskSpec(NamedTuple):
    """单个任务在执行编排中的静态信息（与 tasks 列表索引对齐）。

    config —— 加载期校验通过的任务配置模型；被拒绝的任务为 None
        （blocked），错误日志已在加载期给出，运行期静默跳过。
    task_id —— 规范化后的任务标识；id 缺省或非法时为 None（不可被依赖，
        也不能进入已完成集合）。
    depends_on —— 校验通过的前置任务 id 列表。
    """

    config: TaskConfig | None
    task_id: str | None = None
    depends_on: tuple[str, ...] = ()

    @property
    def blocked(self) -> bool:
        """加载期被拒绝（配置存在依赖类/模型类错误），本轮必须跳过。"""
        return self.config is None


def _normalize_id(task: dict[str, Any], index: int, logger: Logger) -> str | None:
    """规范化任务 id：非法时告警、写回空值并返回 None（任务照常执行，不可被依赖）。

    非法值必须写回为 None：id 非法的语义是"降级为无 id"而非"任务非法"，
    若保留原值，模型校验会以"id 应为字符串"拒绝整个任务。
    """
    raw_id = task.get("id")
    if raw_id is None:
        return None
    if not isinstance(raw_id, str):
        logger.warning(f"任务 #{index + 1} 的 id 配置值非法（应为字符串），视为无 id。")
        task["id"] = None
        return None
    if not ALIAS_PATTERN.match(raw_id):
        logger.warning(
            f"任务 #{index + 1} 的 id '{raw_id}' 含非法字符"
            "（仅允许 a-z、A-Z、0-9、-、_），视为无 id。"
        )
        task["id"] = None
        return None
    return raw_id


def _normalize_depends_on(
    task: dict[str, Any], index: int, logger: Logger
) -> tuple[str, ...] | None:
    """规范化 depends_on；类型非法时记错误并返回 None（调用方置 blocked）。

    归一后的元组写回条目——模型将 depends_on 声明为 tuple[str, ...]，
    字符串原值不写回会被字段校验拒绝。
    """
    raw = task.get("depends_on")
    if raw is None:
        return ()
    if isinstance(raw, str):
        task["depends_on"] = (raw,)
        return (raw,)
    if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        normalized = tuple(raw)
        task["depends_on"] = normalized
        return normalized
    logger.error(
        f"任务 #{index + 1} 的 depends_on 配置值非法（应为字符串或字符串数组），跳过该任务。"
    )
    return None


def _has_unresolvable_reference(
    spec: TaskSpec, index: int, registered: dict[str, int], logger: Logger
) -> bool:
    """依赖引用是否存在无法满足项（此时全部合法 id 已注册）。"""
    for dep in spec.depends_on:
        target = registered.get(dep)
        if target is None:
            logger.error(f"任务 #{index + 1} 依赖的 id '{dep}' 不存在，跳过该任务。")
            return True
        if target == index:
            logger.error(f"任务 #{index + 1} 依赖了自身 (id '{dep}')，跳过该任务。")
            return True
        if target > index:
            logger.error(
                f"任务 #{index + 1} 依赖了位于其后方的任务 '{dep}'"
                "（仅允许引用配置顺序中更早的任务），跳过该任务。"
            )
            return True
    return False


def _norm_path(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _is_within(child: str, ancestor: str) -> bool:
    """child 路径是否等于或位于 ancestor 之内（大小写/分隔符归一后判定）。"""
    child_path = _norm_path(child)
    ancestor_path = _norm_path(ancestor)
    if child_path == ancestor_path:
        return True
    try:
        return os.path.commonpath([child_path, ancestor_path]) == ancestor_path
    except ValueError:
        # 不同盘符（Windows）等无公共祖先的情形
        return False


def _validate_paths(config: TaskConfig, index: int, logger: Logger) -> bool:
    """目录位置关系的跨字段校验；返回 False 表示任务应被拒绝。

    - dest_dir 不得等于或位于 source_dir 之内，source_dir 也不得位于
      dest_dir 之内：任务输出写入扫描树会形成"输出再被扫描"的反馈回路；
    - backup_dir 不得位于 source_dir/dest_dir 之内：成功善后的移动/清理
      会与备份目录互相破坏。
    """
    if _is_within(config.dest_dir, config.source_dir) or _is_within(
        config.source_dir, config.dest_dir
    ):
        logger.error(
            f"任务 #{index + 1} 的 dest_dir 与 source_dir 不得相同或存在嵌套关系，跳过该任务。"
        )
        return False

    if config.backup_dir and (
        _is_within(config.backup_dir, config.source_dir)
        or _is_within(config.backup_dir, config.dest_dir)
    ):
        logger.error(
            f"任务 #{index + 1} 的 backup_dir 不得位于 source_dir 或 dest_dir 之内，跳过该任务。"
        )
        return False

    return True


def resolve_tasks(
    raw_tasks: list[Any],
    *,
    logger: Logger,
) -> list[TaskSpec]:
    """解析任务列表，产出与索引对齐的 TaskSpec（含全部校验日志）。

    第一遍逐任务做 id 规范化与注册（仅合法 id，先到先得——注册反映
    "配置中声明的 id"，与任务自身合法性解耦：被拒绝任务声明的 id 仍可
    被引用，运行期依赖门禁自然级联）、depends_on 类型归一、配置模型
    校验与跨字段约束校验；第二遍在全部合法 id 注册完毕后做
    引用校验（存在性 / 自引用 / 前向引用）。两遍产出与 raw_tasks
    一一对应的 draft，最终按序返回。
    """
    drafts: list[TaskSpec] = []
    registered: dict[str, int] = {}  # id → 首个出现的任务索引
    for index, raw in enumerate(raw_tasks):
        if not isinstance(raw, dict):
            logger.warning(f"任务 #{index + 1} 不是有效的字典结构，将被跳过")
            drafts.append(TaskSpec(None))
            continue

        # 浅拷贝：归一化需要写回（id / depends_on），不污染调用方的原始字典
        entry = dict(raw)

        task_id = _normalize_id(entry, index, logger)
        if task_id is not None and task_id in registered:
            logger.error(
                f"任务 #{index + 1} 的 id '{task_id}' 与任务 #{registered[task_id] + 1}"
                " 重复，跳过该任务。"
            )
            drafts.append(TaskSpec(None))
            continue
        if task_id is not None:
            registered[task_id] = index

        depends_on = _normalize_depends_on(entry, index, logger)
        if depends_on is None:
            drafts.append(TaskSpec(None))
            continue

        try:
            config = TaskConfig.model_validate(entry)
        except ValidationError as e:
            logger.error(f"任务 #{index + 1} 配置无效，跳过该任务：{format_validation_error(e)}")
            drafts.append(TaskSpec(None))
            continue

        if not _validate_paths(config, index, logger):
            drafts.append(TaskSpec(None))
            continue
        if config.remove_source and config.backup_dir:
            logger.warning(f"任务 #{index + 1} 配置了 remove_source = true，backup_dir 将被忽略。")

        drafts.append(TaskSpec(config, task_id, depends_on))

    specs: list[TaskSpec] = []
    for index, draft in enumerate(drafts):
        if draft.blocked:
            specs.append(draft)
        elif _has_unresolvable_reference(draft, index, registered, logger):
            specs.append(TaskSpec(None))
        else:
            specs.append(draft)
    return specs
