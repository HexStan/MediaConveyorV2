"""白盒·原子写入原语（fs.atomic_writer / copy_file / move_file）。

为什么必须白盒（tests/README.md 原则二）：
- EXDEV 回退依赖"跨设备"这一 OS 层错误，只能在 os.replace 边界模拟；
- fsync 先于 rename 的顺序、临时文件的独占创建与残留清理是瞬时中间态，
  从 execute() 入口无法确定性观察；
- 临时文件名是「残留清理」契约的一部分，必须直接钉死。
打桩全部落在 OS 边界，符合原则三；fs 包是无业务依赖的纯原语层，
直接调用即为被测入口。
"""

import errno
import os

import pytest

from src import fs
from src.fs.atomic import PARTIAL_SUFFIX

PARTIAL_NAME = ".a.txt" + PARTIAL_SUFFIX


# ============================================================
# atomic_writer
# ============================================================


class TestAtomicWriter:
    def test_success_replaces_dest_and_leaves_no_residue(self, tmp_path):
        dest = tmp_path / "a.txt"
        dest.write_bytes(b"old")

        with fs.atomic_writer(str(dest)) as f:
            f.write(b"new")

        assert dest.read_bytes() == b"new"
        assert [p.name for p in tmp_path.iterdir()] == ["a.txt"]

    def test_error_keeps_dest_and_cleans_temp(self, tmp_path):
        dest = tmp_path / "a.txt"
        dest.write_bytes(b"old")

        with pytest.raises(RuntimeError), fs.atomic_writer(str(dest)) as f:
            f.write(b"partial")
            raise RuntimeError("boom")

        assert dest.read_bytes() == b"old"
        assert [p.name for p in tmp_path.iterdir()] == ["a.txt"]

    def test_fsyncs_before_replace(self, tmp_path, monkeypatch):
        """fsync 必须发生在 rename 之前——掉电后 rename 才有完整数据可读。"""
        calls = []
        real_fsync = os.fsync
        real_replace = os.replace

        def fake_fsync(fd):
            calls.append("fsync")
            return real_fsync(fd)

        def fake_replace(src, dst):
            calls.append("replace")
            return real_replace(src, dst)

        monkeypatch.setattr(os, "fsync", fake_fsync)
        monkeypatch.setattr(os, "replace", fake_replace)

        dest = tmp_path / "a.txt"
        with fs.atomic_writer(str(dest)) as f:
            f.write(b"data")

        assert calls == ["fsync", "replace"]
        assert dest.read_bytes() == b"data"

    def test_finalize_receives_temp_path_before_replace(self, tmp_path):
        """finalize 收到的是同目录的临时路径（元数据须写入临时文件）。"""
        seen = {}

        def finalize(temp_path):
            seen["temp_exists"] = os.path.exists(temp_path)
            seen["temp_not_dest"] = os.path.abspath(temp_path) != os.path.abspath(
                str(tmp_path / "a.txt")
            )
            seen["temp_same_dir"] = os.path.dirname(os.path.abspath(temp_path)) == os.path.abspath(
                str(tmp_path)
            )

        dest = tmp_path / "a.txt"
        with fs.atomic_writer(str(dest), finalize=finalize) as f:
            f.write(b"data")

        assert seen == {
            "temp_exists": True,
            "temp_not_dest": True,
            "temp_same_dir": True,
        }

    def test_discards_stale_partial_for_same_target(self, tmp_path):
        """再次写入同名文件时，清理上次崩溃残留的临时文件。"""
        stale = tmp_path / PARTIAL_NAME
        stale.write_bytes(b"stale")

        dest = tmp_path / "a.txt"
        with fs.atomic_writer(str(dest)) as f:
            f.write(b"data")

        assert not stale.exists()
        assert dest.read_bytes() == b"data"

    def test_falls_back_to_random_name_when_deterministic_name_is_a_directory(self, tmp_path):
        """确定性名字被目录占用（残留清理失败）时，回退随机名仍能完成写入。"""
        (tmp_path / PARTIAL_NAME).mkdir()

        dest = tmp_path / "a.txt"
        with fs.atomic_writer(str(dest)) as f:
            f.write(b"data")

        assert dest.read_bytes() == b"data"
        assert sorted(p.name for p in tmp_path.iterdir()) == [
            PARTIAL_NAME,
            "a.txt",
        ]


# ============================================================
# copy_file
# ============================================================


class TestCopyFile:
    def test_copies_content_and_preserves_mtime(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_bytes(b"data")
        os.utime(src / "a.txt", (1000000000, 1000000000))

        fs.copy_file(str(src / "a.txt"), str(tmp_path / "dest" / "a.txt"))

        dest = tmp_path / "dest" / "a.txt"
        assert dest.read_bytes() == b"data"
        assert dest.stat().st_mtime == 1000000000

    def test_preserves_mode_on_posix(self, tmp_path):
        if os.name != "posix":
            pytest.skip("POSIX 权限位仅 POSIX 可断言")

        src = tmp_path / "a.txt"
        src.write_bytes(b"data")
        os.chmod(src, 0o600)

        fs.copy_file(str(src), str(tmp_path / "b.txt"))

        assert (tmp_path / "b.txt").stat().st_mode & 0o777 == 0o600

    def test_discards_stale_partial_for_same_target(self, tmp_path):
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()
        (dest_dir / PARTIAL_NAME).write_bytes(b"stale")

        src = tmp_path / "a.txt"
        src.write_bytes(b"data")

        fs.copy_file(str(src), str(dest_dir / "a.txt"))

        assert not (dest_dir / PARTIAL_NAME).exists()
        assert (dest_dir / "a.txt").read_bytes() == b"data"


# ============================================================
# move_file
# ============================================================


class TestMoveFile:
    def test_same_filesystem_move_is_single_replace(self, tmp_path, monkeypatch):
        """同文件系统移动只允许一次 os.replace，不经过临时文件。"""
        src = tmp_path / "a.txt"
        src.write_bytes(b"data")
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()

        replace_calls = []
        real_replace = os.replace

        def fake_replace(s, d):
            replace_calls.append((s, d))
            return real_replace(s, d)

        monkeypatch.setattr(os, "replace", fake_replace)

        fs.move_file(str(src), str(dest_dir / "a.txt"))

        assert replace_calls == [(str(src), str(dest_dir / "a.txt"))]
        assert not src.exists()
        assert (dest_dir / "a.txt").read_bytes() == b"data"

    def test_cross_device_move_falls_back_to_copy_then_delete(self, tmp_path, monkeypatch):
        """EXDEV（跨设备）时回退原子复制，复制成功后才删源。"""
        src = tmp_path / "src" / "a.txt"
        src.parent.mkdir()
        src.write_bytes(b"data")
        os.utime(src, (1000000000, 1000000000))

        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()
        dest = dest_dir / "a.txt"

        real_replace = os.replace

        def fake_replace(s, d):
            if s == str(src) and d == str(dest):
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            return real_replace(s, d)

        monkeypatch.setattr(os, "replace", fake_replace)

        fs.move_file(str(src), str(dest))

        assert not src.exists()
        assert dest.read_bytes() == b"data"
        assert dest.stat().st_mtime == 1000000000
        assert [p.name for p in dest_dir.iterdir()] == ["a.txt"]

    def test_cross_device_move_keeps_source_when_copy_fails(self, tmp_path, monkeypatch):
        """复制失败时源文件必须完好——移动的破坏性动作只能发生在成功之后。"""
        src = tmp_path / "src" / "a.txt"
        src.parent.mkdir()
        src.write_bytes(b"data")

        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()
        dest = dest_dir / "a.txt"

        real_replace = os.replace

        def fake_replace(s, d):
            if s == str(src) and d == str(dest):
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            if s.endswith(PARTIAL_SUFFIX):
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_replace(s, d)

        monkeypatch.setattr(os, "replace", fake_replace)

        with pytest.raises(fs.FsOpError):
            fs.move_file(str(src), str(dest))

        assert src.read_bytes() == b"data"
        assert not dest.exists()

    def test_wraps_non_exdev_errors(self, tmp_path):
        """非 EXDEV 的 OS 错误被包装为 FsOpError，而不是静默吞掉。"""
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()

        with pytest.raises(fs.FsOpError):
            fs.move_file(str(tmp_path / "missing.txt"), str(dest_dir / "a.txt"))
