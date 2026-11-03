"""端到端测试：以子进程驱动 PyInstaller 打包产物（黑盒）。

启用条件：环境变量 MEDIACONVEYOR_BIN 指向打包程序目录（如 dist/MediaConveyor）。
未设置时整个模块跳过——日常源码开发不构建二进制，套件保持全绿；
CI 中由 executable-build.yml 在构建后设置该变量执行。

本模块覆盖灰盒够不到的两段链路：
1. TOML 配置形状 → 任务字典：灰盒直接传入 Python 字典，绕过了 ConfigManager；
   这里走真实的 config.toml 加载路径，钉死规则块在 TOML 中的形状；
2. 打包布局与入口启动：冻结形态下应用根目录 = 可执行文件所在目录
   （src/app/paths.py），配置必须写在程序目录的 config/ 下。

驱动子进程属于进程边界，符合 tests/README.md 原则三。

时钟说明（对原则四的偏离及理由）：打包二进制无法注入 now，只能使用真实
时钟。测试仅依赖「真实当前时刻远超 make_tree 固定的过去时间戳 OLD」这一
单调不变量（NOW = 2023-11-14，早于任何现实运行时刻）；轮转顺序等行为
判定全部来自显式设定的 mtime，不依赖具体时刻，也不 sleep。
"""

import os
import shutil
import subprocess

import pytest

from src.platform_info import exe_name
from tests.helpers import OLD, make_tree, snapshot

PROGRAM_DIR = os.environ.get("MEDIACONVEYOR_BIN")

pytestmark = pytest.mark.skipif(
    not PROGRAM_DIR or not os.path.isdir(PROGRAM_DIR),
    reason="未设置 MEDIACONVEYOR_BIN（指向打包程序目录，如 dist/MediaConveyor），"
    "跳过打包产物端到端测试",
)


def _entry_name():
    """打包入口名：Windows 为 MediaConveyor.exe，其余平台为裸二进制。"""
    return exe_name("MediaConveyor")


def _run_binary(program_dir, timeout=120):
    env = os.environ.copy()
    # 非冻结运行时固定 UTF-8；冻结产物会忽略 PYTHON* 环境变量，
    # 由入口的 _ensure_utf8_stdio 保证输出编码
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return subprocess.run(
        [os.path.join(program_dir, _entry_name())],
        check=False,
        cwd=program_dir,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=timeout,
    )


def _write_config(program_dir, body):
    """在程序目录写入测试用 config.toml（冻结形态从 exe 同级 config/ 读取）。"""
    config_path = os.path.join(program_dir, "config", "config.toml")
    with open(config_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(body)
    return config_path


def _assert_ok(result):
    assert result.returncode == 0, (
        f"退出码 {result.returncode}\n"
        f"---- stdout ----\n{result.stdout}\n"
        f"---- stderr ----\n{result.stderr}"
    )


@pytest.fixture
def program_dir(tmp_path):
    """复制打包程序目录到临时位置：不污染构建产物，且允许写入测试配置。"""
    target = tmp_path / "MediaConveyor"
    shutil.copytree(PROGRAM_DIR, target, ignore=shutil.ignore_patterns("logs"))
    return str(target)


class TestMoveViaBinary:
    def test_move_transfers_old_files(self, program_dir, tmp_path):
        src = (tmp_path / "src").as_posix()
        dest = (tmp_path / "dest").as_posix()
        os.makedirs(dest)  # 目标目录已存在是生产前置条件
        make_tree(tmp_path / "src", {"a.txt": b"alpha", "sub/b.log": b"beta"})
        _write_config(
            program_dir,
            f"""\
log_dir = "./logs"

[[tasks]]
mode = "move"
source = "{src}"
dest = "{dest}"
mtime_threshold_seconds = 3600
remove_empty_dirs = true
max_retries = 1
""",
        )

        result = _run_binary(program_dir)

        _assert_ok(result)
        assert snapshot(tmp_path / "src") == {}
        assert snapshot(tmp_path / "dest") == {"a.txt": 5, "sub/b.log": 4}
        # remove_empty_dirs = true：搬空后的子目录被清理
        assert not os.path.exists(tmp_path / "src" / "sub")

    def test_rule_pipeline_applies_toml_rules(self, program_dir, tmp_path):
        """include 目录命中 → exclude 拦截 → delete 删除的完整流水线，
        顺带钉死规则块在 TOML 中的形状（灰盒直接传字典，覆盖不到）。"""
        src = (tmp_path / "src").as_posix()
        dest = (tmp_path / "dest").as_posix()
        os.makedirs(dest)
        make_tree(
            tmp_path / "src",
            {
                "media/v1.mp4": b"v",
                "media/secret.mp4": b"s",
                "media/junk.tmp": b"j",
                "outside.txt": b"o",
            },
        )
        _write_config(
            program_dir,
            f"""\
log_dir = "./logs"

[[tasks]]
mode = "move"
source = "{src}"
dest = "{dest}"
mtime_threshold_seconds = 3600
remove_empty_dirs = false
max_retries = 1

[tasks.include_rules]
ge."media/" = 0

[tasks.exclude_rules]
ge."secret.mp4" = 0

[tasks.delete_rules]
lt."*.tmp" = "10MB"
""",
        )

        result = _run_binary(program_dir)

        _assert_ok(result)
        assert snapshot(tmp_path / "src") == {"media/secret.mp4": 1, "outside.txt": 1}
        assert snapshot(tmp_path / "dest") == {"media/v1.mp4": 1}


class TestRotateViaBinary:
    def test_count_limit_rotates_oldest(self, program_dir, tmp_path):
        src = (tmp_path / "src").as_posix()
        dest = (tmp_path / "dest").as_posix()
        os.makedirs(dest)
        make_tree(tmp_path / "src", {f"f{i}.txt": (b"x", OLD + i) for i in range(5)})
        _write_config(
            program_dir,
            f"""\
log_dir = "./logs"

[[tasks]]
mode = "rotate"
source = "{src}"
dest = "{dest}"
conflict_policy = "skip"
remove_empty_dirs = false
count_limit = 3
max_retries = 1
""",
        )

        result = _run_binary(program_dir)

        _assert_ok(result)
        # 超过 count_limit=3：最旧的 f0、f1 被轮转出目录
        assert snapshot(tmp_path / "src") == {
            "f2.txt": 1,
            "f3.txt": 1,
            "f4.txt": 1,
        }
        assert snapshot(tmp_path / "dest") == {"f0.txt": 1, "f1.txt": 1}
