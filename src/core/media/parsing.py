"""过滤条件配置值的解析器：容量、时长与码率。

单位语义遵循 IEC 80000-13：KB/MB/GB/TB 按十进制（1KB=1000B）、
KiB/MiB/GiB/TiB 按二进制（1KiB=1024B）；码率同样按十进制
（1Kbps=1000bps，1Mbps=1000_000bps）。裸数字一律按基础单位处理
（容量为字节、时长为秒、码率为 bps）。

解析在配置加载期完成，无效值显式报错——静默降级为 0 会让过滤条件
被静默禁用，属配置错误而非合法缺省。
"""

import re

from pydantic import ByteSize, TypeAdapter, ValidationError

# ByteSize 的内部正则没有尾部锚（"10MB extra" 也会被截断接受），
# 严格模式下先用本模式锚定整串形状，再交给 ByteSize 解析单位表。
_SIZE_SHAPE = re.compile(r"\s*\d*\.?\d+\s*[a-zA-Z]*\s*")

_BYTE_SIZE_ADAPTER: TypeAdapter[ByteSize] = TypeAdapter(ByteSize)

# 时长单位 → 秒（大小写不敏感；支持 "1h30m"/"1h 30m" 组合与 "1.5h" 小数）
_DURATION_UNITS: dict[str, float] = {
    "s": 1.0,
    "sec": 1.0,
    "secs": 1.0,
    "second": 1.0,
    "seconds": 1.0,
    "m": 60.0,
    "min": 60.0,
    "mins": 60.0,
    "minute": 60.0,
    "minutes": 60.0,
    "h": 3600.0,
    "hr": 3600.0,
    "hrs": 3600.0,
    "hour": 3600.0,
    "hours": 3600.0,
    "d": 86400.0,
    "day": 86400.0,
    "days": 86400.0,
}

# 时长令牌：数字 + 可选单位，整串必须全部由令牌组成
_DURATION_TOKEN = re.compile(r"\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]*)\s*")

# 码率令牌：数字 + 可选十进制词头 + 可选 bps/ps/b 后缀（"500K"、"1Mbps"、
# "1000bps"、"1MB" 均可；大小写不敏感）
_BITRATE_SHAPE = re.compile(
    r"\s*(\d+(?:\.\d+)?)\s*([kmgtpKMGTP]?)\s*(?:bps|ps|b)?\s*", re.IGNORECASE
)

_BITRATE_MULTIPLIERS: dict[str, float] = {
    "": 1.0,
    "k": 1e3,
    "m": 1e6,
    "g": 1e9,
    "t": 1e12,
}


class InvalidSizeError(ValueError):
    """容量字符串无法解析（配置错误）。"""


class InvalidDurationError(ValueError):
    """时长字符串无法解析（配置错误）。"""


class InvalidBitrateError(ValueError):
    """码率字符串无法解析（配置错误）。"""


def _reject_bool(value: object, message: str) -> None:
    """布尔值不是合法的容量/时长/码率（pydantic lax 会把 True 强转为 1）。"""
    if isinstance(value, bool):
        # 仅 ValueError 会被 pydantic 校验通道转换为 ValidationError，故不用 TypeError
        raise ValueError(message)  # noqa: TRY004


def parse_size_string(value: object) -> int:
    """把容量配置值解析为字节数（IEC 80000-13：KB=1000，KiB=1024）。

    - 空值视为未配置，返回 0（= 不限制）；
    - 数字直接按字节解释；
    - 其余无法解析的输入抛 InvalidSizeError：配置错误必须显式失败。
    """
    if value is None or value == "":
        return 0
    _reject_bool(value, "容量配置值无效（不接受布尔值）")
    if isinstance(value, (int, float)):
        return int(value)

    s = str(value).strip()
    if not _SIZE_SHAPE.fullmatch(s):
        raise InvalidSizeError(f"容量字符串 {s!r} 无法解析")
    try:
        return int(_BYTE_SIZE_ADAPTER.validate_python(s))
    except ValidationError as e:
        raise InvalidSizeError(f"容量字符串 {s!r} 无法解析") from e


def parse_duration_string(value: object) -> float:
    """把时长配置值解析为秒数。

    - 空值视为未配置，返回 0（= 不限制）；
    - 数字直接按秒解释；
    - 字符串支持 "10s"、"2h"、"1h30m"、"1h 30m"、"1.5h" 等组合形式，
      单位见 _DURATION_UNITS（大小写不敏感）；
    - 其余无法解析的输入抛 InvalidDurationError。
    """
    if value is None or value == "":
        return 0.0
    _reject_bool(value, "时长配置值无效（不接受布尔值）")
    if isinstance(value, (int, float)):
        return float(value)

    s = str(value).strip()
    if not s:
        return 0.0

    total = 0.0
    pos = 0
    for match in _DURATION_TOKEN.finditer(s):
        if match.start() != pos:
            raise InvalidDurationError(f"时长字符串 {s!r} 无法解析")
        pos = match.end()
        number, unit = match.groups()
        multiplier = _DURATION_UNITS.get(unit.lower())
        if multiplier is None:
            raise InvalidDurationError(f"时长字符串 {s!r} 的单位 {unit!r} 无效")
        total += float(number) * multiplier
    if pos != len(s):
        raise InvalidDurationError(f"时长字符串 {s!r} 无法解析")
    return total


def parse_bitrate_string(value: object) -> float:
    """把码率配置值解析为 bps（十进制：1K=1000，1M=1000_000）。

    - 空值视为未配置，返回 0（= 不限制）；
    - 数字直接按 bps 解释；
    - 字符串支持 "500K"、"1Mbps"、"1000bps"、"1MB" 等形式，
      bps/ps/b 后缀可省略（大小写不敏感）；
    - 其余无法解析的输入抛 InvalidBitrateError。
    """
    if value is None or value == "":
        return 0.0
    _reject_bool(value, "码率配置值无效（不接受布尔值）")
    if isinstance(value, (int, float)):
        return float(value)

    s = str(value).strip()
    match = _BITRATE_SHAPE.fullmatch(s)
    if match is None:
        raise InvalidBitrateError(f"码率字符串 {s!r} 无法解析")
    number, prefix = match.groups()
    return float(number) * _BITRATE_MULTIPLIERS[prefix.lower()]
