"""任务配置模型层：TOML 任务条目 → 类型化任务对象。

所有"仅凭配置文件本身即可判定"的任务级合法性校验收口于此：字段
存在性、类型与范围、模式级交叉约束。需要外部条件的校验（目录存在、
依赖任务完成）不在本层——前者由 handlers 在运行期检查，后者由
app.runtime 的任务循环判定。

模型设计：
- 按 mode 判别：``TASK_MODELS`` 维护 mode → 模型类的分派表（解析器
  按 mode 选模型后手工分派，不用 pydantic discriminated union——
  为的是 mode 缺失/不支持时能逐字输出钉死的中文错误消息）；
- ``extra="forbid"``：未知字段（含拼写错误）拒绝任务——配置文件驱动
  的工具里，字段拼错静默失效最难排查；
- ``frozen=True``：模型加载后不可变，处理器只读消费；
- 数字字段接受数字字符串（lax 强转）；布尔值一律拒绝——pydantic lax
  会把 True 强转为 1.0，由 _reject_bool 显式排除。

错误消息约定：测试钉死的文案由 validator 抛中文 ValueError 原样
透出（经 format_validation_error 的 value_error 分支剥前缀）；其余
字段交给类型与范围约束，由 format_validation_error 统一翻译。
"""

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    StringConstraints,
)


def _reject_bool(value: object) -> object:
    """数值字段拒绝布尔值（pydantic lax 会把 True 强转为 1.0，既有校验
    明确排除布尔，语义为配置错误而非数值）。"""
    if isinstance(value, bool):
        # pydantic validator 契约：仅 ValueError 会被转换为 ValidationError，
        # TypeError 会绕过模型校验直接冒泡，故此处必须抛 ValueError
        raise ValueError("配置值无效（不接受布尔值）")  # noqa: TRY004
    return value


class TaskBase(BaseModel):
    """各模式任务的公共字段（mode 由子类以 Literal 收窄并参与分派）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # id 的"非法降级为无 id"语义在解析器（task_plan）处理：降级不是
    # 校验失败，模型只接收归一后的干净值（非法值被写回为 None）
    id: str | None = None
    name: str | None = None
    # 前置任务 id 列表：单字符串 → 单元素元组的归一与跨任务引用校验在
    # 解析器完成，模型只声明归一后的字段形态
    depends_on: tuple[str, ...] = ()
    source_dir: Annotated[str, StringConstraints(min_length=1)]
    dest_dir: str = ""


class TranscodeTaskConfig(TaskBase):
    """transcode 模式任务配置（领域字段在领域移植阶段补齐）。"""

    mode: Literal["transcode"]
    # 覆写基类默认值：转码没有目标目录即无意义，dest_dir 必填
    dest_dir: Annotated[str, StringConstraints(min_length=1)]


TaskConfig = TranscodeTaskConfig

# mode → 配置模型的分派表（解析器使用；与 registry 的 mode → 处理器
# 映射由同一组模式名驱动，不一致属编程错误，registry 有防御分支兜底）
TASK_MODELS: dict[str, type[TaskConfig]] = {
    "transcode": TranscodeTaskConfig,
}
