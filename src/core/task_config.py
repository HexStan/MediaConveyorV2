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
  会把 True 强转为 1.0，由 _reject_bool 显式排除；
- 过滤条件的范围值在加载期解析为数值（parsing 模块）：字符串误写、
  非法单位等在加载期显式报错，而不是运行期被静默当作 0。

错误消息约定：测试钉死的文案由 validator 抛中文 ValueError 原样
透出（经 format_validation_error 的 value_error 分支剥前缀）；其余
字段交给类型与范围约束，由 format_validation_error 统一翻译。
"""

from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from src.core.media.parsing import (
    InvalidBitrateError,
    InvalidDurationError,
    InvalidSizeError,
    parse_bitrate_string,
    parse_duration_string,
    parse_size_string,
)


def _reject_bool(value: object) -> object:
    """数值字段拒绝布尔值（pydantic lax 会把 True 强转为 1.0，既有校验
    明确排除布尔，语义为配置错误而非数值）。"""
    if isinstance(value, bool):
        # pydantic validator 契约：仅 ValueError 会被转换为 ValidationError，
        # TypeError 会绕过模型校验直接冒泡，故此处必须抛 ValueError
        raise ValueError("配置值无效（不接受布尔值）")  # noqa: TRY004
    return value


# ---- 过滤条件的范围值类型（加载期解析为数值） ------------------------------


def _parse_size_value(value: object) -> int:
    try:
        return parse_size_string(value)
    except InvalidSizeError as e:
        raise ValueError(str(e)) from e


def _parse_duration_value(value: object) -> float:
    try:
        return parse_duration_string(value)
    except InvalidDurationError as e:
        raise ValueError(str(e)) from e


def _parse_bitrate_value(value: object) -> float:
    try:
        return parse_bitrate_string(value)
    except InvalidBitrateError as e:
        raise ValueError(str(e)) from e


SizeValue = Annotated[int, BeforeValidator(_parse_size_value)]
DurationValue = Annotated[float, BeforeValidator(_parse_duration_value)]
BitrateValue = Annotated[float, BeforeValidator(_parse_bitrate_value)]
SecondsValue = Annotated[float, BeforeValidator(_reject_bool)]
CountValue = Annotated[int, BeforeValidator(_reject_bool)]


class _Range(BaseModel):
    """范围条件的公共形状：min / max，0（或缺省）表示不限制。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class SizeRange(_Range):
    """size = { min = "100MB", max = "20GB" }（字节）。"""

    min: SizeValue = 0
    max: SizeValue = 0


class DurationRange(_Range):
    """duration = { min = "10s", max = "2h" }（秒）。"""

    min: DurationValue = 0.0
    max: DurationValue = 0.0


class BitrateRange(_Range):
    """video_bitrate / audio_bitrate / total_bitrate（bps，十进制词头）。"""

    min: BitrateValue = 0.0
    max: BitrateValue = 0.0


class FramerateRange(_Range):
    """framerate = { min = 24, max = 60 }（fps）。"""

    min: Annotated[float, BeforeValidator(_reject_bool)] = 0.0
    max: Annotated[float, BeforeValidator(_reject_bool)] = 0.0


class ShortSideRange(_Range):
    """short_side = { min = 720, max = 1080 }（短边像素数）。"""

    min: CountValue = Field(default=0, ge=0)
    max: CountValue = Field(default=0, ge=0)


def _lower_list(value: object) -> object:
    """字符串列表统一转小写（匹配大小写不敏感的既有行为），其余原样放行。"""
    if isinstance(value, list):
        return [item.lower() if isinstance(item, str) else item for item in value]
    return value


class FilterConfig(BaseModel):
    """[tasks.filter] 配置块：文件名流水线 + 媒体属性条件。

    文件名 patterns 在加载期统一小写（与运行期对 rel_path 的归一
    配对，实现大小写不敏感匹配）；编码排除列表同理。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    include_patterns: Annotated[list[str], BeforeValidator(_lower_list)] = Field(
        default_factory=list
    )
    exclude_patterns: Annotated[list[str], BeforeValidator(_lower_list)] = Field(
        default_factory=list
    )
    passthrough_patterns: Annotated[list[str], BeforeValidator(_lower_list)] = Field(
        default_factory=list
    )
    # 修改时间冷却（秒）：文件需早于该阈值才进入处理，0 表示不筛选
    file_mtime: SecondsValue = Field(default=0, ge=0)
    size: SizeRange = Field(default_factory=SizeRange)
    duration: DurationRange = Field(default_factory=DurationRange)
    video_bitrate: BitrateRange = Field(default_factory=BitrateRange)
    audio_bitrate: BitrateRange = Field(default_factory=BitrateRange)
    total_bitrate: BitrateRange = Field(default_factory=BitrateRange)
    framerate: FramerateRange = Field(default_factory=FramerateRange)
    short_side: ShortSideRange = Field(default_factory=ShortSideRange)
    exclude_video_codecs: Annotated[list[str], BeforeValidator(_lower_list)] = Field(
        default_factory=list
    )
    exclude_audio_codecs: Annotated[list[str], BeforeValidator(_lower_list)] = Field(
        default_factory=list
    )


# ---- 任务模型 ---------------------------------------------------------------


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
    """transcode 模式任务配置：监听源目录，用 FFmpeg 处理后按配置善后。"""

    mode: Literal["transcode"]
    # 覆写基类默认值：转码没有目标目录即无意义，dest_dir 必填
    dest_dir: Annotated[str, StringConstraints(min_length=1)]
    filter: FilterConfig = Field(default_factory=FilterConfig)

    # FFmpeg 命令模板（{input}/{output} 占位符），多行书写合法
    ffmpeg_cmd: Annotated[str, StringConstraints(min_length=1)]
    # 主命令连续失败 fallback_count 次后启用的备用命令模板
    ffmpeg_cmd_fallback: str = ""
    fallback_count: CountValue = Field(default=0, ge=0)
    # 单个文件失败次数上限：达到后永久跳过（成功一次即清零）
    failure_count: CountValue = Field(default=3, ge=0)
    # FFmpeg 单文件处理超时（秒），0 表示不限
    ffmpeg_timeout: SecondsValue = Field(default=0, ge=0)
    # 稳定性检查等待秒数：等待后复查文件大小是否仍在变化，0 表示不检查
    stable_duration: SecondsValue = Field(default=0, ge=0)
    # 成功后是否删除源文件；source_expired_minutes > 0 时延迟删除
    remove_source: bool = False
    source_expired_minutes: SecondsValue = Field(default=0, ge=0)
    # 成功后源文件的归档目录（保持子目录结构）；remove_source 为 true 时忽略
    backup_dir: str = ""

    @model_validator(mode="after")
    def _check_fallback_config(self) -> Self:
        """配置了 fallback_count 但没有备用命令：切换条件永不满足，属配置错误。"""
        if self.fallback_count > 0 and not self.ffmpeg_cmd_fallback:
            raise ValueError("配置了 fallback_count 时必须同时配置 ffmpeg_cmd_fallback")
        return self


TaskConfig = TranscodeTaskConfig

# mode → 配置模型的分派表（解析器使用；与 registry 的 mode → 处理器
# 映射由同一组模式名驱动，不一致属编程错误，registry 有防御分支兜底）
TASK_MODELS: dict[str, type[TaskConfig]] = {
    "transcode": TranscodeTaskConfig,
}
