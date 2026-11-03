"""端到端测试：以子进程驱动 PyInstaller 打包产物（黑盒）。

启用条件：环境变量 MEDIACONVEYOR_BIN 指向打包程序目录（如 dist/MediaConveyor）。
未设置时整个模块跳过——日常源码开发不构建二进制，套件保持全绿；
CI 中由 executable-build.yml 在构建后设置该变量执行。

本模块覆盖灰盒够不到的两段链路：
1. TOML 配置形状 → 任务字典：灰盒直接传入 Python 字典，绕过了 ConfigManager；
   这里走真实的 config.toml 加载路径，钉死任务与过滤条件块在 TOML 中的形状；
2. 打包布局与入口启动：冻结形态下应用根目录 = 可执行文件所在目录
   （src/app/paths.py），配置必须写在程序目录的 config/ 下。

驱动子进程属于进程边界，符合 tests/README.md 原则三。

场景设计：既有任务配置 passthrough_patterns 直传（不经 FFmpeg，任意
字节皆可），也有借助包内内置 FFmpeg 的真实转码——FFmpeg 执行语义已由
灰盒测试在进程边界打桩覆盖，直传场景钉的是 TOML 形状与冻结布局；真实
转码场景覆盖"可执行版内置 FFmpeg 可被命令模板解析并完成处理"的链路
（CI runner 不预装 ffmpeg，内置前该链路无法在打包产物上验证）。

时钟说明（对原则四的偏离及理由）：打包二进制无法注入 now，只能使用真实
时钟。测试仅依赖「真实当前时刻远超 make_tree 固定的过去时间戳 OLD」这一
单调不变量（NOW = 2023-11-14，早于任何现实运行时刻）；行为判定不依赖
具体时刻，也不 sleep。
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


def _ffmpeg_task_toml(src, dest, *, filter_body=""):
    """单任务节：直传过滤占位（ffmpeg_cmd 与 output_filename 均为 TOML
    字面量字符串，花括号单写——模板经 str.format 填充后即得真实路径；
    真实执行由 TestBundledFFmpegTranscode 覆盖）。"""
    filter_block = f"\n[tasks.filter]\n{filter_body}" if filter_body else ""
    return (
        f'source_dir = "{src}"\n'
        f'dest_dir = "{dest}"\n'
        "ffmpeg_cmd = 'ffmpeg -y -i {input} {output}'\n"
        'output_filename = "{basename}.mp4"\n'
        f"{filter_block}"
    )


def _bundled_ffmpeg(program_dir):
    """打包产物内的内置 ffmpeg 路径（spec 经 FFMPEG_STAGE 打入 _internal/ffmpeg/）。"""
    return os.path.join(program_dir, "_internal", "ffmpeg", exe_name("ffmpeg"))


@pytest.fixture
def program_dir(tmp_path):
    """复制打包程序目录到临时位置：不污染构建产物，且允许写入测试配置。"""
    target = tmp_path / "MediaConveyor"
    shutil.copytree(PROGRAM_DIR, target, ignore=shutil.ignore_patterns("logs"))
    return str(target)


class TestFFmpegTaskViaBinary:
    def test_passthrough_transfers_files(self, program_dir, tmp_path):
        """[[tasks]] + 嵌套 [tasks.filter] 的 TOML 形状（灰盒直接传字典，
        覆盖不到）：passthrough 命中的文件不经 FFmpeg 直传进目标目录，
        保持子目录结构。"""
        src = (tmp_path / "src").as_posix()
        dest = (tmp_path / "dest").as_posix()
        os.makedirs(dest)  # 目标目录已存在是生产前置条件
        make_tree(tmp_path / "src", {"a.txt": b"alpha", "sub/b.log": b"beta"})
        _write_config(
            program_dir,
            'log_dir = "./logs"\n\n[[tasks]]\n'
            + _ffmpeg_task_toml(
                src, dest, filter_body='passthrough_patterns = ["*.txt", "*.log"]\n'
            ),
        )

        result = _run_binary(program_dir)

        _assert_ok(result)
        assert snapshot(tmp_path / "src") == {}
        assert snapshot(tmp_path / "dest") == {"a.txt": 5, "sub/b.log": 4}
        # 搬空后的子目录被清理
        assert not os.path.exists(tmp_path / "src" / "sub")

    def test_filter_pipeline_applies_toml_rules(self, program_dir, tmp_path):
        """include 门禁 → exclude 拦截 → passthrough 直传的完整流水线：
        include 未命中的文件留在源目录，exclude 命中的被跳过。"""
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
            'log_dir = "./logs"\n\n[[tasks]]\n'
            + _ffmpeg_task_toml(
                src,
                dest,
                filter_body=(
                    'include_patterns = ["media/*"]\n'
                    'exclude_patterns = ["*.tmp", "media/secret.mp4"]\n'
                    'passthrough_patterns = ["*"]\n'
                ),
            ),
        )

        result = _run_binary(program_dir)

        _assert_ok(result)
        assert snapshot(tmp_path / "src") == {
            "media/secret.mp4": 1,
            "media/junk.tmp": 1,
            "outside.txt": 1,
        }
        assert snapshot(tmp_path / "dest") == {"media/v1.mp4": 1}

    def test_multi_task_dependency_gating(self, program_dir, tmp_path):
        """两个 [[tasks]] 节 + id/depends_on 的 TOML 形状：前置任务本轮
        完成后，依赖它的任务才执行。"""
        src_a = (tmp_path / "src-a").as_posix()
        dest_a = (tmp_path / "dest-a").as_posix()
        src_b = (tmp_path / "src-b").as_posix()
        dest_b = (tmp_path / "dest-b").as_posix()
        for d in (dest_a, dest_b):
            os.makedirs(d)
        make_tree(tmp_path / "src-a", {"a.txt": b"alpha"})
        make_tree(tmp_path / "src-b", {"b.txt": b"beta"})
        _write_config(
            program_dir,
            'log_dir = "./logs"\n\n'
            '[[tasks]]\nid = "first"\n'
            + _ffmpeg_task_toml(src_a, dest_a, filter_body='passthrough_patterns = ["*"]\n')
            + '\n[[tasks]]\ndepends_on = ["first"]\n'
            + _ffmpeg_task_toml(src_b, dest_b, filter_body='passthrough_patterns = ["*"]\n'),
        )

        result = _run_binary(program_dir)

        _assert_ok(result)
        assert snapshot(tmp_path / "src-a") == {}
        assert snapshot(tmp_path / "dest-a") == {"a.txt": 5}
        assert snapshot(tmp_path / "src-b") == {}
        assert snapshot(tmp_path / "dest-b") == {"b.txt": 4}


class TestBundledFFmpegTranscode:
    def test_real_transcode_via_bundled_ffmpeg(self, program_dir, tmp_path):
        """内置 FFmpeg 的真实转码链路：先以包内 ffmpeg 合成测试视频
        （lavfi 源，无外部依赖），再经 MediaConveyor 的命令模板转码进
        目标目录——命令模板中的裸 ffmpeg 经 PATH 前置解析到内置版本
        （src/app/paths.py），源文件处理后清空。"""
        bundled = _bundled_ffmpeg(program_dir)
        assert os.path.isfile(bundled), "发行包缺少内置 ffmpeg"

        src = (tmp_path / "src").as_posix()
        dest = (tmp_path / "dest").as_posix()
        os.makedirs(src)
        os.makedirs(dest)
        video = os.path.join(src, "clip.mp4")
        subprocess.run(
            [
                bundled,
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=duration=0.3:size=64x64:rate=10",
                "-pix_fmt",
                "yuv420p",
                video,
            ],
            check=True,
            capture_output=True,
            timeout=120,
        )
        # 合成产物钉到历史时间，绕开写入稳定性等待（同 make_tree 的时钟约定）
        os.utime(video, (OLD, OLD))
        os.utime(src, (OLD, OLD))

        _write_config(
            program_dir,
            # remove_source：FFmpeg 成功后的默认善后是保留源文件（remove_source
            # 默认 False），此处开启删除以断言完整的"消费-产出"链路
            'log_dir = "./logs"\n\n[[tasks]]\n'
            + _ffmpeg_task_toml(src, dest)
            + "remove_source = true\n",
        )

        result = _run_binary(program_dir)

        _assert_ok(result)
        assert snapshot(tmp_path / "src") == {}
        dest_files = snapshot(tmp_path / "dest")
        # 转码产物大小取决于编码器，只断言文件落位且非空
        assert list(dest_files) == ["clip.mp4"]
        assert dest_files["clip.mp4"] > 0
