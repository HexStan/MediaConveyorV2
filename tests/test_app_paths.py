"""app_paths 的路径锚定回归测试。

源码形态下 app_base_dir 必须锚定仓库根（含 main.py 与 config/ 的目录），
而非 src 包内部——包层级重组移动本模块时曾因 __file__ 层级推算漂移，
导致 config/config.toml 被解析到 src/config/ 下，此测试钉死该契约。
"""

from pathlib import Path

import pytest

from src.app import paths

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(
    paths.is_frozen(), reason="冻结形态下应用根为可执行文件目录，契约见 test_e2e_packaged"
)
def test_source_mode_base_dir_is_repo_root():
    assert Path(paths.app_base_dir()).resolve() == PROJECT_ROOT


@pytest.mark.skipif(paths.is_frozen(), reason="同上")
def test_config_path_anchored_at_repo_root():
    assert Path(paths.config_path()) == PROJECT_ROOT / "config" / "config.toml"
