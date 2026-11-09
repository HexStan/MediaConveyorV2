"""共享校验基建：标识符正则与校验错误中文格式化。

pydantic 相关的跨包共享词汇统一收口于此。本模块与 fs/ 平级，是仅依赖
pydantic 与标准库的叶子模块，所有包可导入（分层约定见 AGENTS.md）。

设计约束：
- format_validation_error 只输出字段定位与中文错误短语，绝不输出
  ``err["input"]`` 原值，防止配置中的敏感值落入日志；
- 用户可见的错误短语保持中文，与项目日志惯例一致。
"""

import re

from pydantic import ValidationError

# 任务 id 等标识符共用的合法字符集：a-z、A-Z、0-9、-、_
ALIAS_PATTERN = re.compile(r"^[a-zA-Z0-9_\-]+$")

# pydantic 错误类型 → 中文短语；未收录类型回退 pydantic 原生 msg
_ERROR_TYPE_MESSAGES: dict[str, str] = {
    "missing": "缺少必填字段",
    "string_type": "应为字符串",
    "string_pattern_mismatch": "格式不合法",
    "string_too_short": "不能为空",
    "int_parsing": "数值格式无效",
    "float_parsing": "数值格式无效",
    "int_type": "应为整数",
    "float_type": "应为数字",
    "int_from_float": "应为整数（不接受带小数的数值）",
    "greater_than_equal": "不能小于 {ge}",
    "greater_than": "必须大于 {gt}",
    "less_than_equal": "不能大于 {le}",
    "less_than": "必须小于 {lt}",
    "bool_type": "应为布尔值",
    "dict_type": "应为字典结构",
    "model_type": "应为字典结构",
    "list_type": "应为数组结构",
    "finite_number": "必须为有限数字",
    # Literal 字段（如 conflict_policy）：expected 为 pydantic 生成的取值清单
    "literal_error": "配置值无效，允许的取值: {expected}",
    # extra="forbid"：未知字段（拼写错误、不适配当前模式的字段）
    "extra_forbidden": "不支持的字段",
}


def format_validation_error(exc: ValidationError) -> str:
    """把 ValidationError 汇总为单行中文摘要（字段定位 + 短语，不含原值）。"""
    parts: list[str] = []
    for err in exc.errors():
        err_type = err["type"]
        if err_type == "value_error":
            # 业务 validator 抛出的已是中文消息，剥掉 pydantic 的 "Value error, " 前缀
            message = err["msg"].removeprefix("Value error, ") or "配置值无效"
        else:
            template = _ERROR_TYPE_MESSAGES.get(err_type, err["msg"])
            try:
                message = template.format(**(err.get("ctx") or {}))
            except KeyError, IndexError:
                message = template
        loc = ".".join(str(part) for part in err["loc"])
        # 模型级错误（loc 为空）的消息自解释，不加字段前缀
        parts.append(f"{loc}: {message}" if loc else message)
    return "；".join(parts)
