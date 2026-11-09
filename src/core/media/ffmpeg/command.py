"""FFmpeg 命令组装：output_filename 渲染、参数模板填充与 part 文件命名。

命令组装分两步：output_filename 模板（仅 {basename}）先渲染出输出
文件名，与 dest 子目录结构拼成 {output} 的值；参数模板再以
{input}/{output} 填充。完整命令由三部分拼成：ffmpeg 可执行文件
（经 PATH 解析，冻结形态解析到内置版本）→ 程序预置参数
（FFMPEG_PRESET_ARGS）→ 用户参数模板。占位符的值由本模块按运行
平台自动加引号，模板中无需手写引号；支持项之外的花括号占位符在
填充期显式报错。

输出文件先落瞬态 part 文件名（part_output_filename），成功后由处理
器原子改名复原：观察目标目录的消费者只会看到完整的最终文件。
"""

import shlex

from src.platform_info import is_windows

# 程序注入的 ffmpeg 可执行文件：经 shell 的 PATH 解析；冻结形态由
# paths.ensure_bundled_ffmpeg_on_path 把内置目录前置到 PATH，命令解析到
# 内置版本。二进制不进用户模板：模板只填 FFmpeg 参数。
FFMPEG_BIN = "ffmpeg"

# 程序预置参数：拼在用户参数模板之前，后续新增程序侧命令行注入项只改此处。
# - -y：输出文件已存在时直接覆盖（part 文件机制下正常不会冲突，防极端
#   情形下 ffmpeg 交互式询问挂起进程）；
# - -nostdin：禁用 stdin（ffmpeg 默认读 stdin，前台运行时按键 q 会误停
#   正在进行的转码）；
# - -nostats：关闭 stderr 的周期 stats 行（frame=/time=/speed=），错误
#   输出只剩真正的日志与报错，进度数据改由 progress 协议提供；
# - -progress pipe:1：progress 协议块写入 stdout，经读取线程切块解析
#   （见 execute 模块）。用户模板若自身写 stdout 会与进度块交叠，
#   本项目的文件型输出任务不涉及该形态。
FFMPEG_PRESET_ARGS: tuple[str, ...] = ("-y", "-nostdin", "-nostats", "-progress", "pipe:1")


class TemplateFormatError(ValueError):
    """模板填充失败：命令模板含有 {input}/{output} 之外、output_filename
    模板含有 {basename} 之外的占位符。"""


def _shell_quote(value: str) -> str:
    """按运行平台的 shell 语法为路径加引号（{input}/{output} 值的统一出口）。

    - Windows：命令经 cmd.exe（shell=True）执行，其元字符（& | < > ^ 等）
      在双引号内均按字面处理，故无条件双引号包裹；尾部反斜杠翻倍，防止
      转义收尾引号。%VAR% 展开在引号内仍会发生，路径含成对 % 的极端
      情形不受保护；
    - POSIX（Linux / macOS / Docker）：shlex.quote，覆盖全部不安全字符，
      安全字符不加引号。
    """
    if is_windows():
        if value.endswith("\\"):
            value += "\\"
        return f'"{value}"'
    return shlex.quote(value)


def build_command(template: str, filepath: str, output_path: str) -> str:
    """填充 FFmpeg 参数模板并与程序注入项组装为单行完整命令。

    - 组装顺序：FFMPEG_BIN → FFMPEG_PRESET_ARGS → 用户参数模板，
      {output} 的值因此保持在命令行末尾；
    - ``{input}`` → 源文件完整路径；``{output}`` → 输出文件完整路径
      （文件名由任务的 output_filename 模板渲染，子目录结构随源文件）；
    - 两个占位符的值由程序按运行平台自动加引号，模板中无需（也不应）
      再手写引号；
    - 多行模板合并为单行（换行替换为空格），支持 TOML 多行字符串书写；
    - 模板中的其他花括号占位符会触发 KeyError，属配置错误，显式抛出
      TemplateFormatError（str.format 的既有语义）。
    """
    try:
        raw = template.format(input=_shell_quote(filepath), output=_shell_quote(output_path))
    except (KeyError, IndexError) as e:
        raise TemplateFormatError(
            f"命令模板含有不受支持的占位符: {e}（仅支持 {{input}} 与 {{output}}）"
        ) from e
    args = raw.replace("\n", " ").replace("\r", " ").strip()
    return " ".join((FFMPEG_BIN, *FFMPEG_PRESET_ARGS, args))


def render_output_filename(template: str, basename: str) -> str:
    """渲染 output_filename 文件名模板（目前仅支持 ``{basename}``）。

    渲染失败属配置错误，抛 TemplateFormatError——与命令模板错误一样
    可在加载期判定：渲染结果只取决于模板形状，与真实文件名无关。
    """
    try:
        return template.format(basename=basename)
    except (KeyError, IndexError) as e:
        raise TemplateFormatError(
            f"output_filename 模板含有不受支持的占位符: {e}（仅支持 {{basename}}）"
        ) from e


# FFmpeg 输出瞬态文件名前缀：处理期间 {output} 实际指向
# ``.mc-ffmpeg-part.{output}``。标记不置于尾部（``.{output}.mc-ffmpeg-part``
# 形式）的原因：ffmpeg 依据输出文件的扩展名推断封装格式，未知后缀会
# 触发 "Unable to choose an output format"。与 fs/atomic.py 的 .mc-part
# 同构：目标同目录的确定性命名，处理完成后由处理器原子改名复原。
FFMPEG_PART_PREFIX = ".mc-ffmpeg-part."


def part_output_filename(out_filename: str) -> str:
    """渲染输出文件的瞬态 part 文件名（``.mc-ffmpeg-part.{output}``）。

    output_filename 在加载期已校验为纯文件名，part 文件与最终输出
    同目录同文件系统，完成后的改名（os.replace）是原子操作：观察
    目标目录的消费者在处理完成前不会看到最终文件名。完整原名保留在
    标记之后，扩展名仍位于末尾，ffmpeg 的封装格式推断不受影响。
    """
    return f"{FFMPEG_PART_PREFIX}{out_filename}"
