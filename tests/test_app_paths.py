"""app_paths 的路径锚定回归测试。

源码形态下 app_base_dir 必须锚定仓库根（含 main.py 与 config/ 的目录），
而非 src 包内部——包层级重组移动本模块时曾因 __file__ 层级推算漂移，
导致 config/config.toml 被解析到 src/config/ 下，此测试钉死该契约。

内置 FFmpeg 的解析与 PATH 前置属白盒补盲：行为发生在进程环境变量上，
从 execute() 入口不可观察；冻结标记经 monkeypatch 注入，纯逻辑无子进程
（真实冻结链路由 test_e2e_packaged 的打包产物端到端覆盖）。
"""

import os
import sys
from pathlib import Path

import pytest

from src.app import paths
from src.platform_info import exe_name

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(
    paths.is_frozen(), reason="冻结形态下应用根为可执行文件目录，契约见 test_e2e_packaged"
)
def test_source_mode_base_dir_is_repo_root():
    assert Path(paths.app_base_dir()).resolve() == PROJECT_ROOT


@pytest.mark.skipif(paths.is_frozen(), reason="同上")
def test_config_path_anchored_at_repo_root():
    assert Path(paths.config_path()) == PROJECT_ROOT / "config" / "config.toml"


class TestBundledFFmpeg:
    """bundled_ffmpeg_dir 解析与 ensure_bundled_ffmpeg_on_path 前置契约。"""

    @pytest.fixture
    def fake_bundle(self, tmp_path, monkeypatch):
        """伪造冻结环境：_MEIPASS 指向含 ffmpeg/ffprobe 的临时 _internal。"""
        ffmpeg_dir = tmp_path / "_internal" / "ffmpeg"
        ffmpeg_dir.mkdir(parents=True)
        (ffmpeg_dir / exe_name("ffmpeg")).write_bytes(b"")
        (ffmpeg_dir / exe_name("ffprobe")).write_bytes(b"")
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "_internal"), raising=False)
        return str(ffmpeg_dir)

    def test_bundled_dir_resolved_when_frozen(self, fake_bundle):
        assert paths.bundled_ffmpeg_dir() == fake_bundle

    def test_bundled_dir_none_when_not_frozen(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "frozen", False, raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "_internal"), raising=False)
        assert paths.bundled_ffmpeg_dir() is None

    def test_bundled_dir_none_when_binaries_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "_internal"), raising=False)
        (tmp_path / "_internal" / "ffmpeg").mkdir(parents=True)
        assert paths.bundled_ffmpeg_dir() is None

    def test_path_prepended(self, fake_bundle, monkeypatch):
        monkeypatch.setenv("PATH", os.pathsep.join(["/somewhere/else", "/usr/bin"]))
        assert paths.ensure_bundled_ffmpeg_on_path() is True
        assert os.environ["PATH"].split(os.pathsep)[0] == fake_bundle

    def test_path_prepend_idempotent(self, fake_bundle):
        assert paths.ensure_bundled_ffmpeg_on_path() is True
        assert paths.ensure_bundled_ffmpeg_on_path() is False
        assert os.environ["PATH"].split(os.pathsep).count(fake_bundle) == 1

    def test_path_unchanged_when_not_frozen(self, tmp_path, monkeypatch):
        original = os.pathsep.join(["/somewhere/else", "/usr/bin"])
        monkeypatch.setattr(sys, "frozen", False, raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "_internal"), raising=False)
        monkeypatch.setenv("PATH", original)
        assert paths.ensure_bundled_ffmpeg_on_path() is False
        assert os.environ["PATH"] == original
