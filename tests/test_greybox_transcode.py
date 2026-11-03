"""灰盒测试：transcode 任务以真实文件系统 + 真实状态库为判定终态。

本项目会移动/删除用户的源文件，正确性以"目录树终态 + 状态库记录 +
传给 ffmpeg 的命令行"三者的组合为断言对象（tests/README.md 分层策略）。
FFmpeg 以进程边界打桩（stub_ffmpeg）：同步语义本身不测，只钉死装配、
善后与状态流转。
"""

import os

from src.app.context import AppContext
from src.core.state import file_key, task_key
from tests import helpers
from tests.helpers import make_tree, snapshot

NOW = helpers.NOW
OLD = helpers.OLD
RECENT = helpers.RECENT


def transcode_task(source, dest, **overrides):
    """构造任务字典；过滤维度的键自动归位到 filter 子表。"""
    filter_keys = {
        "include_patterns",
        "exclude_patterns",
        "passthrough_patterns",
        "file_mtime",
        "size",
        "duration",
        "video_bitrate",
        "audio_bitrate",
        "total_bitrate",
        "framerate",
        "short_side",
        "exclude_video_codecs",
        "exclude_audio_codecs",
    }
    filter_cfg = dict(overrides.pop("filter", {}))
    for name in list(overrides):
        if name in filter_keys:
            filter_cfg[name] = overrides.pop(name)

    task = {
        "source_dir": str(source),
        "dest_dir": str(dest),
        "ffmpeg_cmd": 'ffmpeg -y -i "{input}" "{output}-encoded.mp4"',
    }
    if filter_cfg:
        task["filter"] = filter_cfg
    task.update(overrides)
    return task


def state():
    return AppContext.get().require_state()


def key_of(task):
    return task_key(task["source_dir"], task["dest_dir"])


def mark_success(task, filepath, size, mtime, success_time):
    state().mark_success(key_of(task), file_key(filepath), success_time, size, mtime)


# ============================================================
# 基本转码流水线
# ============================================================


class TestTranscode:
    def test_success_keeps_source_and_records_state(self, run_task, tmp_path, stub_ffmpeg):
        calls, _fstate = stub_ffmpeg
        task = transcode_task(tmp_path / "src", tmp_path / "dest")
        make_tree(task["source_dir"], {"a.mp4": b"video"})

        handler = run_task(task, now=NOW)

        # 源文件默认保留在原位置
        assert snapshot(task["source_dir"]) == {"a.mp4": 5}
        # 命令行包含 {input}/{output} 的填充结果（输出不含扩展名）
        assert f'"{task["source_dir"]}\\a.mp4"' in calls[0] or (
            f'"{task["source_dir"]}/a.mp4"' in calls[0]
        )
        assert f'"{os.path.join(task["dest_dir"], "a")}-encoded.mp4"' in calls[0]
        # 状态库记录成功与源文件快照
        success = state().get_success_state(
            key_of(task), file_key(os.path.join(task["source_dir"], "a.mp4"))
        )
        assert success is not None
        assert success.source_size == 5
        assert handler.stats.processed == 1

    def test_subdir_structure_preserved_in_output_path(self, run_task, tmp_path, stub_ffmpeg):
        calls, _fstate = stub_ffmpeg
        task = transcode_task(tmp_path / "src", tmp_path / "dest")
        make_tree(task["source_dir"], {"sub/dir/a.mp4": b"video"})

        run_task(task, now=NOW)

        expected_base = os.path.join(task["dest_dir"], "sub", "dir", "a")
        assert f'"{expected_base}-encoded.mp4"' in calls[0]

    def test_passthrough_moves_directly(self, run_task, tmp_path, stub_ffmpeg):
        calls, _fstate = stub_ffmpeg
        task = transcode_task(tmp_path / "src", tmp_path / "dest", passthrough_patterns=["*.srt"])
        make_tree(task["source_dir"], {"a.srt": b"subs", "b.mp4": b"video"})

        handler = run_task(task, now=NOW)

        assert snapshot(task["dest_dir"]) == {"a.srt": 4}
        assert snapshot(task["source_dir"]) == {"b.mp4": 5}
        # srt 不经 ffmpeg，mp4 经 ffmpeg
        assert len(calls) == 1
        assert handler.stats.passthrough == 1
        assert handler.stats.processed == 1

    def test_template_error_aborts_round_without_burning_counters(
        self, run_task, tmp_path, stub_ffmpeg
    ):
        _calls, _state = stub_ffmpeg
        task = transcode_task(tmp_path / "src", tmp_path / "dest", ffmpeg_cmd="ffmpeg {oops}")
        make_tree(task["source_dir"], {"a.mp4": b"video"})

        run_task(task, now=NOW)

        ctx = AppContext.get()
        assert ctx.logger.has("error", "命令模板无效")
        # 配置级错误不算文件失败
        assert (
            state().get_failure_count(
                key_of(task), file_key(os.path.join(task["source_dir"], "a.mp4"))
            )
            == 0
        )


# ============================================================
# 源文件善后（四种去向）
# ============================================================


class TestDisposition:
    def test_remove_source_immediately(self, run_task, tmp_path, stub_ffmpeg):
        task = transcode_task(tmp_path / "src", tmp_path / "dest", remove_source=True)
        make_tree(task["source_dir"], {"a.mp4": b"video"})

        run_task(task, now=NOW)

        assert snapshot(task["source_dir"]) == {}
        assert (
            state().get_success_state(
                key_of(task), file_key(os.path.join(task["source_dir"], "a.mp4"))
            )
            is not None
        )

    def test_remove_source_delayed(self, run_task, tmp_path, stub_ffmpeg):
        task = transcode_task(
            tmp_path / "src", tmp_path / "dest", remove_source=True, source_expired_minutes=60
        )
        make_tree(task["source_dir"], {"a.mp4": b"video"})

        run_task(task, now=NOW)

        # 文件保留，等待下轮扫描的延迟删除判定
        assert snapshot(task["source_dir"]) == {"a.mp4": 5}
        assert AppContext.get().logger.has("info", "60 分钟后删除")

    def test_backup_preserves_subdir(self, run_task, tmp_path, stub_ffmpeg):
        task = transcode_task(tmp_path / "src", tmp_path / "dest", backup_dir=str(tmp_path / "bak"))
        make_tree(task["source_dir"], {"sub/a.mp4": b"video"})

        handler = run_task(task, now=NOW)

        assert snapshot(str(tmp_path / "bak")) == {"sub/a.mp4": 5}
        assert snapshot(task["source_dir"]) == {}
        assert handler.stats.processed == 1

    def test_keep_in_place_by_default(self, run_task, tmp_path, stub_ffmpeg):
        task = transcode_task(tmp_path / "src", tmp_path / "dest")
        make_tree(task["source_dir"], {"a.mp4": b"video"})

        run_task(task, now=NOW)

        assert snapshot(task["source_dir"]) == {"a.mp4": 5}
        assert snapshot(task["dest_dir"]) == {}

    def test_empty_dirs_cleaned_after_success(self, run_task, tmp_path, stub_ffmpeg):
        task = transcode_task(tmp_path / "src", tmp_path / "dest", remove_source=True)
        make_tree(task["source_dir"], {"sub/a.mp4": b"video"})

        run_task(task, now=NOW)

        # 源文件删除后，空目录连带清理（保留根目录）
        assert not os.path.exists(os.path.join(task["source_dir"], "sub"))
        assert os.path.exists(task["source_dir"])


# ============================================================
# 失败计数与 fallback
# ============================================================


class TestFailures:
    def test_failure_increments_counter(self, run_task, tmp_path, stub_ffmpeg):
        _calls, fstate = stub_ffmpeg
        fstate["returncode"] = 1
        fstate["output"] = b"error line\n"
        task = transcode_task(tmp_path / "src", tmp_path / "dest")
        filepath = os.path.join(task["source_dir"], "a.mp4")
        make_tree(task["source_dir"], {"a.mp4": b"video"})

        handler = run_task(task, now=NOW)

        fkey = file_key(filepath)
        assert state().get_failure_count(key_of(task), fkey) == 1
        assert handler.stats.failed == 1
        assert AppContext.get().logger.has("error", "FFmpeg 处理失败")

    def test_main_failure_counted_for_fallback_switch(self, run_task, tmp_path, stub_ffmpeg):
        _calls, fstate = stub_ffmpeg
        fstate["returncode"] = 1
        task = transcode_task(
            tmp_path / "src",
            tmp_path / "dest",
            fallback_count=3,
            ffmpeg_cmd_fallback='ffmpeg -fallback -i "{input}" "{output}-fb.mp4"',
        )
        filepath = os.path.join(task["source_dir"], "a.mp4")
        make_tree(task["source_dir"], {"a.mp4": b"video"})

        run_task(task, now=NOW)

        fkey = file_key(filepath)
        # 主命令失败专计，不占普通失败配额
        assert state().get_ffmpeg_failure_count(key_of(task), fkey) == 1
        assert state().get_failure_count(key_of(task), fkey) == 0

    def test_fallback_command_used_after_threshold(self, run_task, tmp_path, stub_ffmpeg):
        calls, _fstate = stub_ffmpeg
        task = transcode_task(
            tmp_path / "src",
            tmp_path / "dest",
            fallback_count=2,
            ffmpeg_cmd_fallback='ffmpeg -fallback -i "{input}" "{output}-fb.mp4"',
        )
        filepath = os.path.join(task["source_dir"], "a.mp4")
        make_tree(task["source_dir"], {"a.mp4": b"video"})
        fkey = file_key(filepath)
        state().increment_ffmpeg_failure(key_of(task), fkey)
        state().increment_ffmpeg_failure(key_of(task), fkey)

        run_task(task, now=NOW)

        assert "-fallback" in calls[0]

    def test_fallback_failure_counts_as_normal_failure(self, run_task, tmp_path, stub_ffmpeg):
        _calls, fstate = stub_ffmpeg
        fstate["returncode"] = 1
        task = transcode_task(
            tmp_path / "src",
            tmp_path / "dest",
            fallback_count=1,
            ffmpeg_cmd_fallback='ffmpeg -fallback -i "{input}" "{output}-fb.mp4"',
        )
        filepath = os.path.join(task["source_dir"], "a.mp4")
        make_tree(task["source_dir"], {"a.mp4": b"video"})
        state().increment_ffmpeg_failure(key_of(task), file_key(filepath))

        run_task(task, now=NOW)

        fkey = file_key(filepath)
        assert state().get_failure_count(key_of(task), fkey) == 1
        assert state().get_ffmpeg_failure_count(key_of(task), fkey) == 1

    def test_failure_limit_permanently_skips(self, run_task, tmp_path, stub_ffmpeg):
        calls, _fstate = stub_ffmpeg
        task = transcode_task(tmp_path / "src", tmp_path / "dest", failure_count=2)
        filepath = os.path.join(task["source_dir"], "a.mp4")
        make_tree(task["source_dir"], {"a.mp4": b"video"})
        fkey = file_key(filepath)
        state().increment_failure(key_of(task), fkey)
        state().increment_failure(key_of(task), fkey)

        handler = run_task(task, now=NOW)

        assert calls == []
        assert handler.stats.skipped == 1

    def test_partial_output_cleaned_on_failure(self, run_task, tmp_path, stub_ffmpeg):
        _calls, fstate = stub_ffmpeg
        fstate["returncode"] = 1

        def fake_ffmpeg_writes(cmd):
            dest = os.path.join(str(tmp_path), "dest")
            with open(os.path.join(dest, "a-encoded.mp4"), "wb") as f:
                f.write(b"partial")
            with open(os.path.join(dest, "a.log"), "wb") as f:
                f.write(b"log")

        fstate["on_start"] = fake_ffmpeg_writes
        task = transcode_task(tmp_path / "src", tmp_path / "dest")
        make_tree(task["source_dir"], {"a.mp4": b"video"})
        # 处理前已存在的同名前缀文件不受清理影响
        dest_dir = os.path.join(str(tmp_path), "dest")
        os.makedirs(dest_dir, exist_ok=True)
        with open(os.path.join(dest_dir, "other-encoded.mp4"), "wb") as f:
            f.write(b"other")

        run_task(task, now=NOW)

        assert not os.path.exists(os.path.join(dest_dir, "a-encoded.mp4"))
        assert not os.path.exists(os.path.join(dest_dir, "a.log"))
        assert os.path.exists(os.path.join(dest_dir, "other-encoded.mp4"))


# ============================================================
# 状态判定（防重复处理）
# ============================================================


class TestDeduplication:
    def test_already_processed_skipped(self, run_task, tmp_path, stub_ffmpeg):
        calls, _fstate = stub_ffmpeg
        task = transcode_task(tmp_path / "src", tmp_path / "dest")
        filepath = os.path.join(task["source_dir"], "a.mp4")
        make_tree(task["source_dir"], {"a.mp4": b"video"})
        mark_success(task, filepath, 5, OLD, NOW - 3600)

        run_task(task, now=NOW)

        assert calls == []

    def test_changed_source_reprocessed(self, run_task, tmp_path, stub_ffmpeg):
        calls, _fstate = stub_ffmpeg
        task = transcode_task(tmp_path / "src", tmp_path / "dest")
        filepath = os.path.join(task["source_dir"], "a.mp4")
        # 成功记录：旧大小 4；当前文件大小 5 → 视为变化
        make_tree(task["source_dir"], {"a.mp4": b"video"})
        mark_success(task, filepath, 4, OLD, NOW - 3600)

        run_task(task, now=NOW)

        assert len(calls) == 1
        success = state().get_success_state(key_of(task), file_key(filepath))
        assert success is not None
        assert success.source_size == 5

    def test_failure_counter_survives_success_reprocess(self, run_task, tmp_path, stub_ffmpeg):
        """重处理后失败照样累积到上限（不会因记录曾成功而绕过限制）。"""
        calls, fstate = stub_ffmpeg
        fstate["returncode"] = 1
        task = transcode_task(tmp_path / "src", tmp_path / "dest", failure_count=1)
        filepath = os.path.join(task["source_dir"], "a.mp4")
        make_tree(task["source_dir"], {"a.mp4": b"video"})
        mark_success(task, filepath, 4, OLD, NOW - 3600)  # 旧大小 ≠ 当前 5

        run_task(task, now=NOW)  # 重处理，失败
        assert len(calls) == 1

        # 第二轮：失败计数已达上限，不再尝试
        run_task(task, now=NOW)

        assert len(calls) == 1
        assert state().get_failure_count(key_of(task), file_key(filepath)) == 1


# ============================================================
# 延迟删除
# ============================================================


class TestExpiredCleanup:
    def test_expired_file_deleted_and_record_removed(self, run_task, tmp_path, stub_ffmpeg):
        calls, _fstate = stub_ffmpeg
        task = transcode_task(
            tmp_path / "src", tmp_path / "dest", remove_source=True, source_expired_minutes=1
        )
        filepath = os.path.join(task["source_dir"], "a.mp4")
        make_tree(task["source_dir"], {"a.mp4": b"video"})
        mark_success(task, filepath, 5, OLD, NOW - 3600)  # 1 小时前成功，阈值 1 分钟

        handler = run_task(task, now=NOW)

        assert snapshot(task["source_dir"]) == {}
        assert state().get_success_state(key_of(task), file_key(filepath)) is None
        assert handler.stats.expired == 1
        assert calls == []  # 过期清理不触发转码

    def test_not_yet_expired_kept(self, run_task, tmp_path, stub_ffmpeg):
        task = transcode_task(
            tmp_path / "src", tmp_path / "dest", remove_source=True, source_expired_minutes=60
        )
        filepath = os.path.join(task["source_dir"], "a.mp4")
        make_tree(task["source_dir"], {"a.mp4": b"video"})
        mark_success(task, filepath, 5, OLD, NOW - 120)

        run_task(task, now=NOW)

        assert snapshot(task["source_dir"]) == {"a.mp4": 5}

    def test_vanished_expired_file_record_cleaned(
        self, run_task, app_context, tmp_path, stub_ffmpeg
    ):
        """扫描与删除之间文件消失（竞态）：清记录、不报错（白盒驱动清理步骤）。"""
        task = transcode_task(
            tmp_path / "src", tmp_path / "dest", remove_source=True, source_expired_minutes=1
        )
        filepath = os.path.join(task["source_dir"], "a.mp4")
        make_tree(task["source_dir"], {"a.mp4": b"video"})
        mark_success(task, filepath, 5, OLD, NOW - 3600)
        os.remove(filepath)

        handler = run_task(task, now=NOW)
        assert handler is not None
        # 模拟"扫描时文件还在、删除时已消失"的竞态窗口
        handler._cleanup_expired(app_context, [("a.mp4", filepath, file_key(filepath))])

        assert state().get_success_state(key_of(task), file_key(filepath)) is None
        assert not app_context.logger.has("error", "删除过期源文件失败")


# ============================================================
# 扫描过滤
# ============================================================


class TestScanFiltering:
    def test_recent_mtime_skipped(self, run_task, tmp_path, stub_ffmpeg):
        calls, _fstate = stub_ffmpeg
        task = transcode_task(tmp_path / "src", tmp_path / "dest", filter={"file_mtime": 300})
        make_tree(task["source_dir"], {"a.mp4": (b"video", RECENT)})

        run_task(task, now=NOW)

        assert calls == []
        assert snapshot(task["source_dir"]) == {"a.mp4": 5}

    def test_old_mtime_processed(self, run_task, tmp_path, stub_ffmpeg):
        calls, _fstate = stub_ffmpeg
        task = transcode_task(tmp_path / "src", tmp_path / "dest", filter={"file_mtime": 300})
        make_tree(task["source_dir"], {"a.mp4": (b"video", OLD)})

        run_task(task, now=NOW)

        assert len(calls) == 1

    def test_size_filter_without_ffprobe(self, run_task, tmp_path, stub_ffmpeg, monkeypatch):
        """size 过滤不触发 ffprobe：探测函数一旦被调用即测试失败。"""
        calls, _fstate = stub_ffmpeg

        def no_probe(*args, **kwargs):
            raise AssertionError("不应调用 ffprobe")

        monkeypatch.setattr("src.core.media.probe.subprocess.run", no_probe)
        task = transcode_task(tmp_path / "src", tmp_path / "dest", filter={"size": {"min": "1KB"}})
        make_tree(task["source_dir"], {"small.mp4": b"tiny", "big.mp4": b"v" * 2048})

        handler = run_task(task, now=NOW)

        assert len(calls) == 1  # 只有 big.mp4
        assert handler.stats.processed == 1

    def test_duration_filter_with_ffprobe(self, run_task, tmp_path, stub_ffmpeg, monkeypatch):
        probe_calls: list[list] = []

        def fake_run(cmd, **kwargs):
            probe_calls.append(cmd)
            result = type("R", (), {})()
            result.returncode = 0
            result.stdout = '{"format": {"duration": "30.0"}, "streams": []}'
            return result

        monkeypatch.setattr("src.core.media.probe.subprocess.run", fake_run)
        calls, _fstate = stub_ffmpeg
        task = transcode_task(
            tmp_path / "src", tmp_path / "dest", filter={"duration": {"min": "60s"}}
        )
        make_tree(task["source_dir"], {"short.mp4": b"a", "b.mp4": b"b"})

        handler = run_task(task, now=NOW)

        # 两个候选文件都探测过，但只有……都不满足 min=60s → 全部跳过
        assert calls == []
        assert len(probe_calls) == 2
        assert handler.stats.processed == 0

    def test_missing_source_warns_once(self, run_task, tmp_path, stub_ffmpeg):
        task = transcode_task(tmp_path / "no-such-dir", tmp_path / "dest")

        run_task(task, now=NOW)
        run_task(task, now=NOW)

        ctx = AppContext.get()
        warnings = [m for lv, m in ctx.logger.messages if lv == "warning" and "源目录不存在" in m]
        assert len(warnings) == 1

    def test_idle_monitoring_logged_once_per_idle_streak(self, run_task, tmp_path, stub_ffmpeg):
        task = transcode_task(tmp_path / "src", tmp_path / "dest")
        make_tree(task["source_dir"], {})

        run_task(task, now=NOW)
        run_task(task, now=NOW)
        ctx = AppContext.get()
        idle_logs = [m for lv, m in ctx.logger.messages if lv == "info" and "继续监听" in m]
        assert len(idle_logs) == 1

        # 有文件处理后复位：再次空闲会重新提示
        make_tree(task["source_dir"], {"a.srt": b"x"})
        task2 = transcode_task(task["source_dir"], task["dest_dir"], passthrough_patterns=["*"])
        run_task(task2, now=NOW)
        run_task(task, now=NOW)

        idle_logs = [m for lv, m in ctx.logger.messages if lv == "info" and "继续监听" in m]
        assert len(idle_logs) == 2


# ============================================================
# 稳定性与停止
# ============================================================


class TestStabilityAndStop:
    def test_size_change_skips_round(self, run_task, tmp_path, stub_ffmpeg, monkeypatch):
        calls, _fstate = stub_ffmpeg
        task = transcode_task(tmp_path / "src", tmp_path / "dest", stable_duration=0.05)
        make_tree(task["source_dir"], {"a.mp4": b"video"})

        sizes = iter([100, 200])
        monkeypatch.setattr("src.core.handlers.transcode.os.path.getsize", lambda path: next(sizes))

        handler = run_task(task, now=NOW)

        assert calls == []
        assert handler.stats.processed == 0
        assert AppContext.get().logger.has("info", "正在变化")

    def test_file_vanishing_during_check_skips(self, run_task, tmp_path, stub_ffmpeg, monkeypatch):
        calls, _fstate = stub_ffmpeg
        task = transcode_task(tmp_path / "src", tmp_path / "dest", stable_duration=0.05)
        make_tree(task["source_dir"], {"a.mp4": b"video"})

        monkeypatch.setattr(
            "src.core.handlers.transcode.os.path.getsize",
            lambda path: (_ for _ in ()).throw(OSError("gone")),
        )

        run_task(task, now=NOW)

        assert calls == []
        assert AppContext.get().logger.has("warning", "已不存在")

    def test_fast_stop_breaks_loop_between_files(self, run_task, tmp_path, stub_ffmpeg):
        """fast 停止在文件边界优雅退出：不抛异常、不启动新转码、源文件原样。"""
        calls, _fstate = stub_ffmpeg
        task = transcode_task(tmp_path / "src", tmp_path / "dest")
        make_tree(task["source_dir"], {"a.mp4": b"video"})

        # 模拟 stop --fast 已到达：文件循环在边界直接中断
        AppContext.get().lifecycle.request_abort()

        run_task(task, now=NOW)

        assert calls == []
        assert snapshot(task["source_dir"]) == {"a.mp4": 5}


# ============================================================
# 多任务隔离
# ============================================================


class TestTaskIsolation:
    def test_overlapping_source_dirs_do_not_pollute_each_other(
        self, run_task, tmp_path, stub_ffmpeg
    ):
        calls, _fstate = stub_ffmpeg
        task_a = transcode_task(tmp_path / "src", tmp_path / "dest-a")
        task_b = transcode_task(tmp_path / "src", tmp_path / "dest-b")
        filepath = os.path.join(task_a["source_dir"], "a.mp4")
        make_tree(task_a["source_dir"], {"a.mp4": b"video"})
        # 任务 A 已成功处理该文件
        mark_success(task_a, filepath, 5, OLD, NOW - 60)

        run_task(task_a, now=NOW)
        run_task(task_b, now=NOW)

        # 任务 B 的状态键独立，仍会处理该文件
        assert len(calls) == 1
