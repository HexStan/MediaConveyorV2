"""单元测试：SQLite 状态库（防重复处理 / 失败计数 / fallback 切换的依据）。

钉死的契约：
- 记录以 (task_key, filepath) 隔离：不同任务互不串扰；
- mark_success 同时清零两个失败计数，并记录源文件 size/mtime；
- 未成功过的文件（无记录或仅有失败计数）没有 success state。
"""

import os

import pytest

from src.core.state import StateManager, file_key, task_key


@pytest.fixture
def state(tmp_path):
    mgr = StateManager(str(tmp_path / "state.db"))
    yield mgr
    mgr.close()


KEY = task_key("/data/source", "/data/dest")
OTHER_KEY = task_key("/data/source2", "/data/dest2")


class TestKeys:
    def test_task_key_stable_across_relative_forms(self, tmp_path, monkeypatch):
        """相对/绝对形式归一到同一键（配置重排序与工作目录无关）。"""
        monkeypatch.chdir(tmp_path)
        a = task_key("./source", "./dest")
        b = task_key(str(tmp_path / "source"), str(tmp_path / "dest"))
        assert a == b

    def test_file_key_normcases_path(self):
        if os.name == "nt":
            # Windows：NTFS 大小写不敏感，normcase 折叠大小写后指向同一条记录
            assert file_key("C:\\Media\\Video.MP4") == file_key("c:\\media\\video.mp4")
        else:
            # POSIX：文件系统默认大小写敏感，normcase 为恒等，键保持区分
            assert file_key("/Media/Video.MP4") != file_key("/Media/video.mp4")


class TestFailureCounters:
    def test_defaults_to_zero(self, state):
        assert state.get_failure_count(KEY, file_key("/a.mp4")) == 0
        assert state.get_ffmpeg_failure_count(KEY, file_key("/a.mp4")) == 0

    def test_increment_accumulates(self, state):
        fkey = file_key("/a.mp4")

        assert state.increment_failure(KEY, fkey) == 1
        assert state.increment_failure(KEY, fkey) == 2
        assert state.get_failure_count(KEY, fkey) == 2

    def test_ffmpeg_counter_is_independent(self, state):
        fkey = file_key("/a.mp4")

        state.increment_ffmpeg_failure(KEY, fkey)
        assert state.get_ffmpeg_failure_count(KEY, fkey) == 1
        assert state.get_failure_count(KEY, fkey) == 0

    def test_tasks_are_isolated(self, state):
        fkey = file_key("/a.mp4")

        state.increment_failure(KEY, fkey)
        assert state.get_failure_count(OTHER_KEY, fkey) == 0

    def test_counters_survive_reopen(self, tmp_path):
        db_path = str(tmp_path / "state.db")
        fkey = file_key("/a.mp4")

        mgr = StateManager(db_path)
        mgr.increment_failure(KEY, fkey)
        mgr.close()

        mgr2 = StateManager(db_path)
        try:
            assert mgr2.get_failure_count(KEY, fkey) == 1
        finally:
            mgr2.close()


class TestSuccessState:
    def test_no_record_returns_none(self, state):
        assert state.get_success_state(KEY, file_key("/a.mp4")) is None

    def test_failure_only_record_returns_none(self, state):
        fkey = file_key("/a.mp4")
        state.increment_failure(KEY, fkey)

        assert state.get_success_state(KEY, fkey) is None

    def test_mark_success_records_snapshot_and_clears_counters(self, state):
        fkey = file_key("/a.mp4")
        state.increment_failure(KEY, fkey)
        state.increment_ffmpeg_failure(KEY, fkey)

        state.mark_success(KEY, fkey, 1700.0, 4096, 1699.0)

        success = state.get_success_state(KEY, fkey)
        assert success is not None
        assert success.success_time == 1700.0
        assert success.source_size == 4096
        assert success.source_mtime == 1699.0
        assert state.get_failure_count(KEY, fkey) == 0
        assert state.get_ffmpeg_failure_count(KEY, fkey) == 0

    def test_mark_success_overwrites_previous(self, state):
        fkey = file_key("/a.mp4")

        state.mark_success(KEY, fkey, 1000.0, 100, 999.0)
        state.mark_success(KEY, fkey, 2000.0, 200, 1999.0)

        success = state.get_success_state(KEY, fkey)
        assert success is not None
        assert success.success_time == 2000.0
        assert success.source_size == 200

    def test_changed_file_detected_via_snapshot(self, state):
        """重处理判定的依据：快照 size/mtime 与当前不符即视为变化。"""
        fkey = file_key("/a.mp4")
        state.mark_success(KEY, fkey, 1000.0, 100, 999.0)

        success = state.get_success_state(KEY, fkey)
        assert success is not None
        assert (200, 1999.0) != (success.source_size, success.source_mtime)


class TestDeleteRecord:
    def test_delete_existing(self, state):
        fkey = file_key("/a.mp4")
        state.mark_success(KEY, fkey, 1000.0, 100, 999.0)

        state.delete_record(KEY, fkey)

        assert state.get_success_state(KEY, fkey) is None
        assert state.get_failure_count(KEY, fkey) == 0

    def test_delete_missing_is_noop(self, state):
        state.delete_record(KEY, file_key("/ghost.mp4"))

    def test_delete_is_task_scoped(self, state):
        fkey = file_key("/a.mp4")
        state.mark_success(KEY, fkey, 1000.0, 100, 999.0)

        state.delete_record(OTHER_KEY, fkey)

        assert state.get_success_state(KEY, fkey) is not None


class TestSchema:
    def test_db_directory_created(self, tmp_path):
        db_path = str(tmp_path / "nested" / "state.db")
        mgr = StateManager(db_path)
        try:
            assert os.path.isfile(db_path)
        finally:
            mgr.close()
