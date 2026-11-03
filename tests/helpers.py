"""灰盒测试的目录树工具：声明式建树、快照断言、确定性时间常量。

设计约定见 tests/README.md「基础设施」一节。
"""

import os

# 确定性时间基准：所有涉及 mtime / now 的测试一律相对这三个常量取值，
# 禁止使用 time.time() 或依赖文件系统的默认时间戳。
NOW = 1_700_000_000  # 测试中的"当前时刻"（epoch 秒）
OLD = NOW - 30 * 86400  # 30 天前——足以通过任何常规时间阈值
RECENT = NOW - 60  # 60 秒前——不足以通过常规秒级时间阈值


def make_tree(root, files, default_mtime=OLD):
    """按声明式规格创建目录树。

    Args:
        root: 根目录（str 或 Path），不存在会自动创建。
        files: {相对路径: 内容} 映射。相对路径用正斜杠分隔（自动转换为本机分隔符）；
            内容为 bytes，或 (bytes, mtime) 元组以单独指定该文件的 mtime。
        default_mtime: 未单独指定时的文件 mtime（epoch 秒），默认 OLD；
            所有目录（含根目录自身）的 mtime 也统一钉到该值（目录决策取内容物
            最新 mtime，单独指定了更新 mtime 的文件仍然会让目录表现为"新"；
            source_quiet_seconds 的静默判定会读取根目录 mtime，同样必须钉死）。

    Returns:
        str: root 的字符串形式。
    """
    root = str(root)
    os.makedirs(root, exist_ok=True)
    for rel, spec in files.items():
        if isinstance(spec, tuple):
            content, mtime = spec
        else:
            content, mtime = spec, default_mtime

        path = os.path.join(root, *rel.split("/"))
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "wb") as f:
            f.write(content)
        os.utime(path, (mtime, mtime))

    # 写文件会刷新目录 mtime，目录的时间戳必须在所有文件写完后统一设置
    # （根目录自身也参与 source_quiet_seconds 的静默判定，一并钉死）
    for dirpath, dirnames, _filenames in os.walk(root):
        for d in dirnames:
            p = os.path.join(dirpath, d)
            os.utime(p, (default_mtime, default_mtime))
    os.utime(root, (default_mtime, default_mtime))
    return root


def snapshot(root):
    """目录树快照：{相对路径(正斜杠): 文件大小}。

    空目录不出现在快照中（需要断言空目录时直接用 os.path.isdir）。
    root 不存在时返回空 dict，便于对"目标目录未创建"做统一断言。
    """
    result = {}
    if not os.path.isdir(root):
        return result
    for dirpath, _dirs, filenames in os.walk(root):
        for name in filenames:
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            result[rel] = os.path.getsize(path)
    return result
