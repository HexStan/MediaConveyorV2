"""灰盒测试：FFmpeg 任务以真实文件系统 + 真实状态库为判定终态。

本项目会移动/删除用户的源文件，正确性以"目录树终态 + 状态库记录 +
传给 ffmpeg 的命令行"三者的组合为断言对象（tests/README.md 分层策略）。
FFmpeg 以进程边界打桩（stub_ffmpeg）：同步语义本身不测，只钉死装配、
善后与状态流转。
"""

import io
import os
import subprocess

import pytest

from src.app.context import AppContext
from src.core.state import file_key, task_key
from src.fs import FsOpError, TransferAbortedError
from tests import helpers
from tests.helpers import make_tree, snapshot

NOW = helpers.NOW
OLD = helpers.OLD
RECENT = helpers.RECENT


def ffmpeg_task(source, dest, **overrides):
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
        "ffmpeg_cmd": "-i {input} {output}",
        "output_filename": "{basename}-encoded.mp4",
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
# 基本处理流水线
# ============================================================


class TestFFmpegTask:
    def test_success_keeps_source_and_records_state(self, run_task, tmp_path, stub_ffmpeg):
        calls, _fstate = stub_ffmpeg
        task = ffmpeg_task(tmp_path / "src", tmp_path / "dest")
        make_tree(task["source_dir"], {"a.mp4": b"video"})

        handler = run_task(task, now=NOW)

        # 源文件默认保留在原位置
        assert snapshot(task["source_dir"]) == {"a.mp4": 5}
        # 命令行包含 {input}/{output} 的填充结果（引号由程序按平台添加，
        # 引或不引均包含裸路径子串）；{output} 指向瞬态 part 文件而非最终名
        # 程序注入项在前：ffmpeg 可执行文件与预置参数拼在用户参数模板之前
        assert calls[0].startswith("ffmpeg -y -nostdin ")
        assert os.path.join(task["source_dir"], "a.mp4") in calls[0]
        part_output = os.path.join(task["dest_dir"], ".mc-ffmpeg-part.a-encoded.mp4")
        assert part_output in calls[0]
        assert os.path.join(task["dest_dir"], "a-encoded.mp4") not in calls[0]
        # 成功后 part 文件已原子改名为最终输出文件
        assert snapshot(task["dest_dir"]) == {"a-encoded.mp4": 7}
        # 状态库记录成功与源文件快照
        success = state().get_success_state(
            key_of(task), file_key(os.path.join(task["source_dir"], "a.mp4"))
        )
        assert success is not None
        assert success.source_size == 5
        assert handler.stats.processed == 1

    def test_subdir_structure_preserved_in_output_path(self, run_task, tmp_path, stub_ffmpeg):
        calls, _fstate = stub_ffmpeg
        task = ffmpeg_task(tmp_path / "src", tmp_path / "dest")
        make_tree(task["source_dir"], {"sub/dir/a.mp4": b"video"})

        run_task(task, now=NOW)

        part_output = os.path.join(task["dest_dir"], "sub", "dir", ".mc-ffmpeg-part.a-encoded.mp4")
        assert part_output in calls[0]
        assert os.path.join(task["dest_dir"], "sub", "dir", "a-encoded.mp4") not in calls[0]
        # 改名复原后子目录结构与最终文件名不受影响
        assert snapshot(os.path.join(task["dest_dir"], "sub", "dir")) == {"a-encoded.mp4": 7}

    def test_probe_duration_drives_status_line(self, run_task, tmp_path, stub_ffmpeg, monkeypatch):
        """get_media_info 探测到的时长优先驱动状态行：横幅即使在场也不参与解析。"""
        monkeypatch.setattr("src.core.media.ffmpeg.execute._PROGRESS_LOG_INTERVAL", 0)

        def fake_run(cmd, **kwargs):
            result = type("R", (), {})()
            result.returncode = 0
            result.stdout = '{"format": {"duration": "30.0"}, "streams": []}'
            return result

        # duration 过滤触发探测：探测不到时长（0）时文件会被筛掉，借此
        # 保证进入处理的文件必已携带探测时长
        monkeypatch.setattr("src.core.media.probe.subprocess.run", fake_run)
        _calls, fstate = stub_ffmpeg
        fstate["hangs"] = True
        fstate["output"] = (
            b"Input #0, mov,mp4,m4a,3gp,3gpp,mj2, from 'input.mp4':\n"
            b"  Duration: 00:00:08.40, start: 0.000000, bitrate: 8543 kb/s\n"
        )
        fstate["progress"] = (
            b"frame=10\nfps=2.0\nbitrate=128.0kbits/s\ntotal_size=2048\n"
            b"out_time=00:00:01.000000\ndup_frames=0\ndrop_frames=0\nspeed=2.0x\n"
            b"progress=continue\n"
        )
        task = ffmpeg_task(
            tmp_path / "src",
            tmp_path / "dest",
            filter={"duration": {"min": "10s"}},
            ffmpeg_timeout=0.3,
        )
        make_tree(task["source_dir"], {"a.mp4": b"video"})

        run_task(task, now=NOW)

        ctx = AppContext.get()
        progress_logs = [m for lv, m in ctx.logger.messages if lv == "info" and "进行中" in m]
        assert progress_logs
        # 探测值 30 秒生效（out_time = 1 秒）；横幅的 8.40 不再生效
        # （锚定输出时间戳分母位，避免与预计总耗时估算撞形）
        assert any("输出时间戳：00:01 / 00:30" in m for m in progress_logs)
        assert not any("输出时间戳：00:01 / 00:08" in m for m in progress_logs)

    def test_passthrough_moves_directly(self, run_task, tmp_path, stub_ffmpeg):
        calls, _fstate = stub_ffmpeg
        task = ffmpeg_task(tmp_path / "src", tmp_path / "dest", passthrough_patterns=["*.srt"])
        make_tree(task["source_dir"], {"a.srt": b"subs", "b.mp4": b"video"})

        handler = run_task(task, now=NOW)

        assert snapshot(task["dest_dir"]) == {"a.srt": 4, "b-encoded.mp4": 7}
        assert snapshot(task["source_dir"]) == {"b.mp4": 5}
        # srt 不经 ffmpeg，mp4 经 ffmpeg
        assert len(calls) == 1
        assert handler.stats.passthrough == 1
        assert handler.stats.processed == 1
        # 直传成功：源文件已移至目标目录、离开源树，不留记录
        assert (
            state().get_success_state(
                key_of(task), file_key(os.path.join(task["source_dir"], "a.srt"))
            )
            is None
        )

    def test_template_error_aborts_round_without_burning_counters(
        self, run_task, tmp_path, stub_ffmpeg
    ):
        _calls, _state = stub_ffmpeg
        task = ffmpeg_task(tmp_path / "src", tmp_path / "dest", ffmpeg_cmd="{oops}")
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
        task = ffmpeg_task(tmp_path / "src", tmp_path / "dest", remove_source=True)
        make_tree(task["source_dir"], {"a.mp4": b"video"})

        run_task(task, now=NOW)

        assert snapshot(task["source_dir"]) == {}
        # 源文件已离开源树，删除义务了结：不留记录
        assert (
            state().get_success_state(
                key_of(task), file_key(os.path.join(task["source_dir"], "a.mp4"))
            )
            is None
        )

    def test_remove_source_delete_failure_retries_next_round(
        self, run_task, tmp_path, stub_ffmpeg, monkeypatch
    ):
        """立即删除失败：成功记录保留为待删除凭证，下轮自动重试删除。"""
        import src.core.handlers.ffmpeg_task as handler_module

        calls, _fstate = stub_ffmpeg
        task = ffmpeg_task(tmp_path / "src", tmp_path / "dest", remove_source=True)
        filepath = os.path.join(task["source_dir"], "a.mp4")
        make_tree(task["source_dir"], {"a.mp4": b"video"})

        def broken_delete(path):
            raise FsOpError("删除失败")

        real_delete = handler_module._fs_delete_file
        monkeypatch.setattr(handler_module, "_fs_delete_file", broken_delete)
        run_task(task, now=NOW)

        # 删除失败：文件仍在，记录承载待删除义务
        assert snapshot(task["source_dir"]) == {"a.mp4": 5}
        assert state().get_success_state(key_of(task), file_key(filepath)) is not None

        monkeypatch.setattr(handler_module, "_fs_delete_file", real_delete)
        # 成功时间以真实时钟（time.time）写入，第二轮同样用真实时钟，
        # 立即删除模式的记录才会"存在即已到期"
        handler = run_task(task)

        # 下轮到期重试：删除不触发 FFmpeg，文件与记录一并清除
        assert len(calls) == 1
        assert snapshot(task["source_dir"]) == {}
        assert state().get_success_state(key_of(task), file_key(filepath)) is None
        assert handler.stats.expired == 1

    def test_remove_source_delayed(self, run_task, tmp_path, stub_ffmpeg):
        task = ffmpeg_task(
            tmp_path / "src", tmp_path / "dest", remove_source=True, source_expired_minutes=60
        )
        make_tree(task["source_dir"], {"a.mp4": b"video"})

        run_task(task, now=NOW)

        # 文件保留，等待下轮扫描的延迟删除判定
        assert snapshot(task["source_dir"]) == {"a.mp4": 5}
        assert AppContext.get().logger.has("info", "60 分钟后删除")

    def test_backup_preserves_subdir(self, run_task, tmp_path, stub_ffmpeg):
        task = ffmpeg_task(tmp_path / "src", tmp_path / "dest", backup_dir=str(tmp_path / "bak"))
        make_tree(task["source_dir"], {"sub/a.mp4": b"video"})

        handler = run_task(task, now=NOW)

        assert snapshot(str(tmp_path / "bak")) == {"sub/a.mp4": 5}
        assert snapshot(task["source_dir"]) == {}
        assert handler.stats.processed == 1
        # 备份成功：源文件离开源树，义务了结不留记录
        assert (
            state().get_success_state(
                key_of(task), file_key(os.path.join(task["source_dir"], "sub", "a.mp4"))
            )
            is None
        )

    def test_keep_in_place_by_default(self, run_task, tmp_path, stub_ffmpeg):
        task = ffmpeg_task(tmp_path / "src", tmp_path / "dest")
        make_tree(task["source_dir"], {"a.mp4": b"video"})

        run_task(task, now=NOW)

        assert snapshot(task["source_dir"]) == {"a.mp4": 5}
        assert snapshot(task["dest_dir"]) == {"a-encoded.mp4": 7}

    def test_empty_dirs_cleaned_after_success(self, run_task, tmp_path, stub_ffmpeg):
        task = ffmpeg_task(tmp_path / "src", tmp_path / "dest", remove_source=True)
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
        task = ffmpeg_task(tmp_path / "src", tmp_path / "dest")
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
        task = ffmpeg_task(
            tmp_path / "src",
            tmp_path / "dest",
            fallback_count=3,
            ffmpeg_cmd_fallback="-fallback -i {input} {output}",
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
        task = ffmpeg_task(
            tmp_path / "src",
            tmp_path / "dest",
            fallback_count=2,
            ffmpeg_cmd_fallback="-fallback -i {input} {output}",
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
        task = ffmpeg_task(
            tmp_path / "src",
            tmp_path / "dest",
            fallback_count=1,
            ffmpeg_cmd_fallback="-fallback -i {input} {output}",
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
        task = ffmpeg_task(tmp_path / "src", tmp_path / "dest", failure_count=2)
        filepath = os.path.join(task["source_dir"], "a.mp4")
        make_tree(task["source_dir"], {"a.mp4": b"video"})
        fkey = file_key(filepath)
        state().increment_failure(key_of(task), fkey)
        state().increment_failure(key_of(task), fkey)

        handler = run_task(task, now=NOW)

        assert calls == []
        assert handler.stats.skipped == 1

    def test_zero_failure_count_never_skips(self, run_task, tmp_path, stub_ffmpeg):
        """failure_count = 0 表示永不因失败跳过：失败文件每轮重试，计数持续累积。"""
        calls, fstate = stub_ffmpeg
        fstate["returncode"] = 1
        task = ffmpeg_task(tmp_path / "src", tmp_path / "dest", failure_count=0)
        filepath = os.path.join(task["source_dir"], "a.mp4")
        make_tree(task["source_dir"], {"a.mp4": b"video"})

        first = run_task(task, now=NOW)
        second = run_task(task, now=NOW)

        assert len(calls) == 2
        assert first.stats.failed == 1
        assert second.stats.failed == 1
        assert second.stats.skipped == 0
        assert state().get_failure_count(key_of(task), file_key(filepath)) == 2

    def test_partial_output_cleaned_on_failure(self, run_task, tmp_path, stub_ffmpeg):
        _calls, fstate = stub_ffmpeg
        fstate["returncode"] = 1

        def fake_ffmpeg_writes(cmd):
            dest = os.path.join(str(tmp_path), "dest")
            with open(os.path.join(dest, ".mc-ffmpeg-part.a-encoded.mp4"), "wb") as f:
                f.write(b"partial")
            with open(os.path.join(dest, "a.log"), "wb") as f:
                f.write(b"log")

        fstate["on_start"] = fake_ffmpeg_writes
        task = ffmpeg_task(tmp_path / "src", tmp_path / "dest")
        make_tree(task["source_dir"], {"a.mp4": b"video"})
        # 处理前已存在的最终输出文件不受任何影响（FFmpeg 只写 part 文件）
        dest_dir = os.path.join(str(tmp_path), "dest")
        os.makedirs(dest_dir, exist_ok=True)
        with open(os.path.join(dest_dir, "a-encoded.mp4"), "wb") as f:
            f.write(b"previous")

        run_task(task, now=NOW)

        # 失败清理精确作用于 part 文件：本轮残骸被删，
        # 旁路写出的 a.log 与处理前已存在的最终输出均不动
        assert not os.path.exists(os.path.join(dest_dir, ".mc-ffmpeg-part.a-encoded.mp4"))
        assert os.path.exists(os.path.join(dest_dir, "a.log"))
        with open(os.path.join(dest_dir, "a-encoded.mp4"), "rb") as f:
            assert f.read() == b"previous"


# ============================================================
# 失败隔离目录（failure_dir）
# ============================================================


class TestFailureDir:
    def test_limit_reached_file_moved_to_failure_dir(self, run_task, tmp_path, stub_ffmpeg):
        """达到失败上限的文件在下一轮被移入 failure_dir，记录随之清除。"""
        calls, fstate = stub_ffmpeg
        fstate["returncode"] = 1
        task = ffmpeg_task(
            tmp_path / "src",
            tmp_path / "dest",
            failure_count=2,
            failure_dir=str(tmp_path / "failed"),
        )
        filepath = os.path.join(task["source_dir"], "a.mp4")
        make_tree(task["source_dir"], {"a.mp4": b"video"})
        state().increment_failure(key_of(task), file_key(filepath))  # 此前已失败 1 次

        first = run_task(task, now=NOW)

        # 第一轮：再次失败，计数达到上限，文件仍在源目录
        assert len(calls) == 1
        assert snapshot(task["source_dir"]) == {"a.mp4": 5}
        assert state().get_failure_count(key_of(task), file_key(filepath)) == 2
        assert first.stats.failed == 1

        # 第二轮：不再触发 FFmpeg，直接移入失败目录
        second = run_task(task, now=NOW)

        assert len(calls) == 1
        assert snapshot(task["source_dir"]) == {}
        assert snapshot(str(tmp_path / "failed")) == {"a.mp4": 5}
        # 义务了结：源文件离开源树，失败记录随之清除
        assert state().get_failure_count(key_of(task), file_key(filepath)) == 0
        assert second.stats.evicted == 1
        assert second.stats.failed == 0
        assert AppContext.get().logger.has("info", "已移入失败目录")

    def test_failure_dir_preserves_subdir(self, run_task, tmp_path, stub_ffmpeg):
        task = ffmpeg_task(
            tmp_path / "src",
            tmp_path / "dest",
            failure_count=1,
            failure_dir=str(tmp_path / "failed"),
        )
        filepath = os.path.join(task["source_dir"], "sub", "a.mp4")
        make_tree(task["source_dir"], {"sub/a.mp4": b"video"})
        state().increment_failure(key_of(task), file_key(filepath))

        run_task(task, now=NOW)

        assert snapshot(str(tmp_path / "failed")) == {"sub/a.mp4": 5}
        assert snapshot(task["source_dir"]) == {}

    def test_replaced_file_after_eviction_processed_fresh(self, run_task, tmp_path, stub_ffmpeg):
        """隔离后放回源路径的同名文件从头处理：失败记录已随移动清除。"""
        calls, fstate = stub_ffmpeg
        task = ffmpeg_task(
            tmp_path / "src",
            tmp_path / "dest",
            failure_count=1,
            failure_dir=str(tmp_path / "failed"),
        )
        filepath = os.path.join(task["source_dir"], "a.mp4")
        make_tree(task["source_dir"], {"a.mp4": b"video"})
        state().increment_failure(key_of(task), file_key(filepath))

        run_task(task, now=NOW)  # 达到上限，移入失败目录
        assert snapshot(str(tmp_path / "failed")) == {"a.mp4": 5}

        fstate["returncode"] = 0
        make_tree(task["source_dir"], {"a.mp4": b"fixed"})
        run_task(task, now=NOW)

        # 新文件正常处理，而非被再次隔离
        assert len(calls) == 1
        assert snapshot(task["dest_dir"]) == {"a-encoded.mp4": 7}
        assert snapshot(task["source_dir"]) == {"a.mp4": 5}

    def test_zero_failure_count_never_evicts(self, run_task, tmp_path, stub_ffmpeg):
        """failure_count = 0 表示永不因失败跳过：failure_dir 不生效，文件每轮重试。"""
        calls, fstate = stub_ffmpeg
        fstate["returncode"] = 1
        task = ffmpeg_task(
            tmp_path / "src",
            tmp_path / "dest",
            failure_count=0,
            failure_dir=str(tmp_path / "failed"),
        )
        make_tree(task["source_dir"], {"a.mp4": b"video"})

        run_task(task, now=NOW)
        run_task(task, now=NOW)

        assert len(calls) == 2
        assert snapshot(task["source_dir"]) == {"a.mp4": 5}
        assert not os.path.exists(str(tmp_path / "failed"))

    def test_move_failure_retries_next_round(self, run_task, tmp_path, stub_ffmpeg):
        """移动失败（隔离目录不可用）：源文件与记录保留，下轮自动重试。"""
        blocker = tmp_path / "failed"  # 同名文件挡路：ensure_dir 必然失败
        blocker.write_bytes(b"x")
        task = ffmpeg_task(
            tmp_path / "src",
            tmp_path / "dest",
            failure_count=1,
            failure_dir=str(blocker),
        )
        filepath = os.path.join(task["source_dir"], "a.mp4")
        make_tree(task["source_dir"], {"a.mp4": b"video"})
        state().increment_failure(key_of(task), file_key(filepath))

        run_task(task, now=NOW)

        assert snapshot(task["source_dir"]) == {"a.mp4": 5}
        assert state().get_failure_count(key_of(task), file_key(filepath)) == 1
        assert AppContext.get().logger.has("error", "移入失败目录失败")

        blocker.unlink()
        handler = run_task(task, now=NOW)

        assert snapshot(str(tmp_path / "failed")) == {"a.mp4": 5}
        assert snapshot(task["source_dir"]) == {}
        assert state().get_failure_count(key_of(task), file_key(filepath)) == 0
        assert handler.stats.evicted == 1

    def test_vanished_eviction_file_record_cleaned(
        self, run_task, app_context, tmp_path, stub_ffmpeg
    ):
        """扫描与移动之间文件消失（竞态）：清记录、不报错（白盒驱动清理步骤）。"""
        task = ffmpeg_task(
            tmp_path / "src",
            tmp_path / "dest",
            failure_count=1,
            failure_dir=str(tmp_path / "failed"),
        )
        filepath = os.path.join(task["source_dir"], "a.mp4")
        make_tree(task["source_dir"], {"a.mp4": b"video"})
        fkey = file_key(filepath)
        state().increment_failure(key_of(task), fkey)
        os.remove(filepath)

        handler = run_task(task, now=NOW)
        assert handler is not None
        # 模拟"扫描时文件还在、移动时已消失"的竞态窗口
        handler._evict_failed(app_context, [("a.mp4", filepath, fkey)])

        assert state().get_failure_count(key_of(task), fkey) == 0
        assert not app_context.logger.has("error", "移入失败目录失败")


# ============================================================
# part 文件生命周期（瞬态命名 → 原子复原 / 清理）
# ============================================================


class TestPartFileLifecycle:
    def test_stale_part_file_discarded_before_processing(self, run_task, tmp_path, stub_ffmpeg):
        """上次崩溃残留的同名 part 文件在处理启动前被清理。"""
        _calls, fstate = stub_ffmpeg
        fstate["returncode"] = 1  # 失败且不产出任何文件
        task = ffmpeg_task(tmp_path / "src", tmp_path / "dest")
        make_tree(task["source_dir"], {"a.mp4": b"video"})
        dest_dir = os.path.join(str(tmp_path), "dest")
        os.makedirs(dest_dir, exist_ok=True)
        stale_part = os.path.join(dest_dir, ".mc-ffmpeg-part.a-encoded.mp4")
        with open(stale_part, "wb") as f:
            f.write(b"stale")

        run_task(task, now=NOW)

        assert not os.path.exists(stale_part)

    def test_rename_failure_counts_as_plain_failure(
        self, run_task, tmp_path, stub_ffmpeg, monkeypatch
    ):
        """改名失败按普通文件失败处理：不烧 fallback 专用计数，part 随之清理。"""

        def locked_replace(src, dst):
            raise FsOpError("目标被占用")

        monkeypatch.setattr("src.core.handlers.ffmpeg_task.move_file", locked_replace)
        task = ffmpeg_task(tmp_path / "src", tmp_path / "dest")
        filepath = os.path.join(task["source_dir"], "a.mp4")
        make_tree(task["source_dir"], {"a.mp4": b"video"})

        handler = run_task(task, now=NOW)

        fkey = file_key(filepath)
        assert state().get_failure_count(key_of(task), fkey) == 1
        assert state().get_ffmpeg_failure_count(key_of(task), fkey) == 0
        assert handler.stats.failed == 1
        assert not os.path.exists(os.path.join(task["dest_dir"], ".mc-ffmpeg-part.a-encoded.mp4"))

    def test_fast_stop_cleans_part_file(self, run_task, tmp_path, stub_ffmpeg):
        """fast 停止打断 FFmpeg：写入中的 part 文件被清理，源文件原样。"""
        _calls, fstate = stub_ffmpeg

        def abort_and_write_part(cmd):
            dest = os.path.join(str(tmp_path), "dest")
            os.makedirs(dest, exist_ok=True)
            with open(os.path.join(dest, ".mc-ffmpeg-part.a-encoded.mp4"), "wb") as f:
                f.write(b"partial")
            AppContext.get().lifecycle.request_abort()

        fstate["on_start"] = abort_and_write_part
        task = ffmpeg_task(tmp_path / "src", tmp_path / "dest")
        make_tree(task["source_dir"], {"a.mp4": b"video"})

        with pytest.raises(TransferAbortedError):
            run_task(task, now=NOW)

        assert not os.path.exists(os.path.join(task["dest_dir"], ".mc-ffmpeg-part.a-encoded.mp4"))
        assert snapshot(task["source_dir"]) == {"a.mp4": 5}

    def test_abort_race_does_not_publish_part_file(
        self, run_task, tmp_path, stub_ffmpeg, monkeypatch
    ):
        """中止在 FFmpeg 运行中落地且子进程被回收（返回码被映射为 0）：
        收尾终审以中止为准——产物不发布、源文件留守、part 清理、不烧计数。"""
        _calls, _fstate = stub_ffmpeg
        coordinator = AppContext.get().lifecycle

        def reaped_popen(cmd, **kwargs):
            dest = os.path.join(str(tmp_path), "dest")
            os.makedirs(dest, exist_ok=True)
            with open(os.path.join(dest, ".mc-ffmpeg-part.a-encoded.mp4"), "wb") as f:
                f.write(b"partial")

            class ReapedPopen:
                def __init__(self):
                    self.stderr = io.BytesIO(b"")
                    self.stdout = io.BytesIO(b"")
                    self.pid = 4321
                    self.returncode = 0

                def poll(self):
                    # 中止在轮询间隙落地；子进程被回收后 waitpid 命中
                    # ECHILD，subprocess 把返回码映射为 0
                    coordinator.request_abort()
                    return self.returncode

                def wait(self, timeout=None):
                    return self.returncode

            return ReapedPopen()

        monkeypatch.setattr(subprocess, "Popen", reaped_popen)
        task = ffmpeg_task(tmp_path / "src", tmp_path / "dest")
        filepath = os.path.join(task["source_dir"], "a.mp4")
        make_tree(task["source_dir"], {"a.mp4": b"video"})

        with pytest.raises(TransferAbortedError):
            run_task(task, now=NOW)

        assert not os.path.exists(os.path.join(task["dest_dir"], "a-encoded.mp4"))
        assert not os.path.exists(os.path.join(task["dest_dir"], ".mc-ffmpeg-part.a-encoded.mp4"))
        assert snapshot(task["source_dir"]) == {"a.mp4": 5}
        fkey = file_key(filepath)
        assert state().get_failure_count(key_of(task), fkey) == 0
        assert state().get_ffmpeg_failure_count(key_of(task), fkey) == 0


# ============================================================
# 状态判定（防重复处理）
# ============================================================


class TestDeduplication:
    def test_already_processed_skipped(self, run_task, tmp_path, stub_ffmpeg):
        calls, _fstate = stub_ffmpeg
        task = ffmpeg_task(tmp_path / "src", tmp_path / "dest")
        filepath = os.path.join(task["source_dir"], "a.mp4")
        make_tree(task["source_dir"], {"a.mp4": b"video"})
        mark_success(task, filepath, 5, OLD, NOW - 3600)

        run_task(task, now=NOW)

        assert calls == []

    def test_changed_source_reprocessed(self, run_task, tmp_path, stub_ffmpeg):
        calls, _fstate = stub_ffmpeg
        task = ffmpeg_task(tmp_path / "src", tmp_path / "dest")
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
        task = ffmpeg_task(tmp_path / "src", tmp_path / "dest", failure_count=1)
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
# 待删除清理（延迟删除到期，含立即删除模式的失败重试）
# ============================================================


class TestExpiredCleanup:
    def test_expired_file_deleted_and_record_removed(self, run_task, tmp_path, stub_ffmpeg):
        calls, _fstate = stub_ffmpeg
        task = ffmpeg_task(
            tmp_path / "src", tmp_path / "dest", remove_source=True, source_expired_minutes=1
        )
        filepath = os.path.join(task["source_dir"], "a.mp4")
        make_tree(task["source_dir"], {"a.mp4": b"video"})
        mark_success(task, filepath, 5, OLD, NOW - 3600)  # 1 小时前成功，阈值 1 分钟

        handler = run_task(task, now=NOW)

        assert snapshot(task["source_dir"]) == {}
        assert state().get_success_state(key_of(task), file_key(filepath)) is None
        assert handler.stats.expired == 1
        assert calls == []  # 过期清理不触发 FFmpeg

    def test_not_yet_expired_kept(self, run_task, tmp_path, stub_ffmpeg):
        task = ffmpeg_task(
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
        task = ffmpeg_task(
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
        assert not app_context.logger.has("error", "删除源文件失败")


# ============================================================
# 扫描过滤
# ============================================================


class TestScanFiltering:
    def test_recent_mtime_skipped(self, run_task, tmp_path, stub_ffmpeg):
        calls, _fstate = stub_ffmpeg
        task = ffmpeg_task(tmp_path / "src", tmp_path / "dest", filter={"file_mtime": 300})
        make_tree(task["source_dir"], {"a.mp4": (b"video", RECENT)})

        run_task(task, now=NOW)

        assert calls == []
        assert snapshot(task["source_dir"]) == {"a.mp4": 5}

    def test_old_mtime_processed(self, run_task, tmp_path, stub_ffmpeg):
        calls, _fstate = stub_ffmpeg
        task = ffmpeg_task(tmp_path / "src", tmp_path / "dest", filter={"file_mtime": 300})
        make_tree(task["source_dir"], {"a.mp4": (b"video", OLD)})

        run_task(task, now=NOW)

        assert len(calls) == 1

    def test_size_filter_without_ffprobe(self, run_task, tmp_path, stub_ffmpeg, monkeypatch):
        """size 过滤不触发 ffprobe：探测函数一旦被调用即测试失败。"""
        calls, _fstate = stub_ffmpeg

        def no_probe(*args, **kwargs):
            raise AssertionError("不应调用 ffprobe")

        monkeypatch.setattr("src.core.media.probe.subprocess.run", no_probe)
        task = ffmpeg_task(tmp_path / "src", tmp_path / "dest", filter={"size": {"min": "1KB"}})
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
        task = ffmpeg_task(tmp_path / "src", tmp_path / "dest", filter={"duration": {"min": "60s"}})
        make_tree(task["source_dir"], {"short.mp4": b"a", "b.mp4": b"b"})

        handler = run_task(task, now=NOW)

        # 两个候选文件都探测过，但只有……都不满足 min=60s → 全部跳过
        assert calls == []
        assert len(probe_calls) == 2
        assert handler.stats.processed == 0

    def test_missing_source_warns_once(self, run_task, tmp_path, stub_ffmpeg):
        task = ffmpeg_task(tmp_path / "no-such-dir", tmp_path / "dest")

        run_task(task, now=NOW)
        run_task(task, now=NOW)

        ctx = AppContext.get()
        warnings = [m for lv, m in ctx.logger.messages if lv == "warning" and "源目录不存在" in m]
        assert len(warnings) == 1


# ============================================================
# 稳定性与停止
# ============================================================


class TestStabilityAndStop:
    def test_size_change_skips_round(self, run_task, tmp_path, stub_ffmpeg, monkeypatch):
        calls, _fstate = stub_ffmpeg
        task = ffmpeg_task(tmp_path / "src", tmp_path / "dest", stable_duration=0.05)
        make_tree(task["source_dir"], {"a.mp4": b"video"})

        sizes = iter([100, 200])
        monkeypatch.setattr(
            "src.core.handlers.ffmpeg_task.os.path.getsize", lambda path: next(sizes)
        )

        handler = run_task(task, now=NOW)

        assert calls == []
        assert handler.stats.processed == 0
        assert AppContext.get().logger.has("info", "正在变化")

    def test_file_vanishing_during_check_skips(self, run_task, tmp_path, stub_ffmpeg, monkeypatch):
        calls, _fstate = stub_ffmpeg
        task = ffmpeg_task(tmp_path / "src", tmp_path / "dest", stable_duration=0.05)
        make_tree(task["source_dir"], {"a.mp4": b"video"})

        monkeypatch.setattr(
            "src.core.handlers.ffmpeg_task.os.path.getsize",
            lambda path: (_ for _ in ()).throw(OSError("gone")),
        )

        run_task(task, now=NOW)

        assert calls == []
        assert AppContext.get().logger.has("warning", "已不存在")

    def test_fast_stop_breaks_loop_between_files(self, run_task, tmp_path, stub_ffmpeg):
        """fast 停止在文件边界优雅退出：不抛异常、不启动新处理、源文件原样。"""
        calls, _fstate = stub_ffmpeg
        task = ffmpeg_task(tmp_path / "src", tmp_path / "dest")
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
        task_a = ffmpeg_task(tmp_path / "src", tmp_path / "dest-a")
        task_b = ffmpeg_task(tmp_path / "src", tmp_path / "dest-b")
        filepath = os.path.join(task_a["source_dir"], "a.mp4")
        make_tree(task_a["source_dir"], {"a.mp4": b"video"})
        # 任务 A 已成功处理该文件
        mark_success(task_a, filepath, 5, OLD, NOW - 60)

        run_task(task_a, now=NOW)
        run_task(task_b, now=NOW)

        # 任务 B 的状态键独立，仍会处理该文件
        assert len(calls) == 1


# ============================================================
# 处理顺序（process_order）
# ============================================================

# 固定测试树：四种大小 × 四个 mtime，六种取值的处理顺序两两不同。
# mtime 从旧到新：x < b < y < c；size 从大到小：b(400) > y(300) > c(200) > x(100)。
# 扫描顺序（default）= [b, c, x, y]；alphabetic = [x, y, b, c]（子目录排前）。
_ORDER_TREE = {
    "b.mp4": (b"x" * 400, OLD + 86400),
    "c.mp4": (b"x" * 200, NOW),
    "a/x.mp4": (b"x" * 100, OLD),
    "a/y.mp4": (b"x" * 300, OLD + 2 * 86400),
}
_ORDER_NAMES = ("b.mp4", "c.mp4", "x.mp4", "y.mp4")


def _call_names(calls, names):
    """stub 记录的命令行 → 被处理文件的 basename 序列（即处理顺序）。"""
    order = []
    for call in calls:
        matched = [name for name in names if name in call]
        assert len(matched) == 1, f"命令行无法唯一对应文件: {call}"
        order.append(matched[0])
    return order


def _logged_order():
    """捕获日志中的逐文件处理序列（【i/total】准备处理: {rel_path}）。"""
    order = []
    for _level, msg in AppContext.get().logger.messages:
        if "准备处理: " in msg:
            order.append(msg.split("准备处理: ", 1)[1].replace(os.sep, "/"))
    return order


class TestProcessOrder:
    def test_default_keeps_scan_order(self, run_task, tmp_path, stub_ffmpeg):
        calls, _fstate = stub_ffmpeg
        task = ffmpeg_task(tmp_path / "src", tmp_path / "dest")
        make_tree(task["source_dir"], _ORDER_TREE)

        run_task(task, now=NOW)

        # 扫描顺序：根目录文件名排序在前（b, c），随后子目录 a（x, y）
        assert _call_names(calls, _ORDER_NAMES) == ["b.mp4", "c.mp4", "x.mp4", "y.mp4"]

    def test_alphabetic_orders_by_rel_path(self, run_task, tmp_path, stub_ffmpeg):
        calls, _fstate = stub_ffmpeg
        task = ffmpeg_task(tmp_path / "src", tmp_path / "dest", process_order="alphabetic")
        make_tree(task["source_dir"], _ORDER_TREE)

        run_task(task, now=NOW)

        # 相对路径字典序：子目录 a 的文件排在前——与扫描顺序相反
        assert _call_names(calls, _ORDER_NAMES) == ["x.mp4", "y.mp4", "b.mp4", "c.mp4"]

    def test_alphabetic_case_insensitive(self, run_task, tmp_path, stub_ffmpeg):
        calls, _fstate = stub_ffmpeg
        task = ffmpeg_task(tmp_path / "src", tmp_path / "dest", process_order="alphabetic")
        make_tree(task["source_dir"], {"B.mp4": b"x" * 10, "a.mp4": b"x" * 10})

        run_task(task, now=NOW)

        # 比较前统一 casefold：a.mp4 在 B.mp4 之前（码点序会把 B 排前）
        assert _call_names(calls, ("a.mp4", "B.mp4")) == ["a.mp4", "B.mp4"]

    def test_largest_first(self, run_task, tmp_path, stub_ffmpeg):
        calls, _fstate = stub_ffmpeg
        task = ffmpeg_task(tmp_path / "src", tmp_path / "dest", process_order="largest_first")
        make_tree(task["source_dir"], _ORDER_TREE)

        run_task(task, now=NOW)

        assert _call_names(calls, _ORDER_NAMES) == ["b.mp4", "y.mp4", "c.mp4", "x.mp4"]

    def test_smallest_first(self, run_task, tmp_path, stub_ffmpeg):
        calls, _fstate = stub_ffmpeg
        task = ffmpeg_task(tmp_path / "src", tmp_path / "dest", process_order="smallest_first")
        make_tree(task["source_dir"], _ORDER_TREE)

        run_task(task, now=NOW)

        assert _call_names(calls, _ORDER_NAMES) == ["x.mp4", "c.mp4", "y.mp4", "b.mp4"]

    def test_newest_first(self, run_task, tmp_path, stub_ffmpeg):
        calls, _fstate = stub_ffmpeg
        task = ffmpeg_task(tmp_path / "src", tmp_path / "dest", process_order="newest_first")
        make_tree(task["source_dir"], _ORDER_TREE)

        run_task(task, now=NOW)

        assert _call_names(calls, _ORDER_NAMES) == ["c.mp4", "y.mp4", "b.mp4", "x.mp4"]

    def test_oldest_first(self, run_task, tmp_path, stub_ffmpeg):
        calls, _fstate = stub_ffmpeg
        task = ffmpeg_task(tmp_path / "src", tmp_path / "dest", process_order="oldest_first")
        make_tree(task["source_dir"], _ORDER_TREE)

        run_task(task, now=NOW)

        assert _call_names(calls, _ORDER_NAMES) == ["x.mp4", "b.mp4", "y.mp4", "c.mp4"]

    def test_size_ties_keep_scan_order(self, run_task, tmp_path, stub_ffmpeg):
        calls, _fstate = stub_ffmpeg
        task = ffmpeg_task(tmp_path / "src", tmp_path / "dest", process_order="largest_first")
        make_tree(task["source_dir"], {"m.mp4": b"x" * 100, "z.mp4": b"x" * 50, "a.mp4": b"x" * 50})

        run_task(task, now=NOW)

        # 稳定排序：同为 50 字节的 a/z 保持扫描顺序（文件名字典序）
        assert _call_names(calls, ("m.mp4", "z.mp4", "a.mp4")) == ["m.mp4", "a.mp4", "z.mp4"]

    def test_passthrough_joins_ordering(self, run_task, tmp_path, stub_ffmpeg):
        calls, _fstate = stub_ffmpeg
        task = ffmpeg_task(
            tmp_path / "src",
            tmp_path / "dest",
            passthrough_patterns=["*.srt"],
            process_order="alphabetic",
        )
        make_tree(
            task["source_dir"],
            {"b.mp4": b"x" * 20, "z.srt": b"x" * 5, "a/x.mp4": b"x" * 10},
        )

        run_task(task, now=NOW)

        # 直传文件与 FFmpeg 文件在同一队列一起排序；日志序列含全部文件
        assert _logged_order() == ["a/x.mp4", "b.mp4", "z.srt"]
        # ffmpeg 仅被 mp4 文件调用，直传不经过 ffmpeg
        assert _call_names(calls, ("b.mp4", "x.mp4")) == ["x.mp4", "b.mp4"]
        assert snapshot(task["dest_dir"]) == {
            "a/x-encoded.mp4": 7,
            "b-encoded.mp4": 7,
            "z.srt": 5,
        }
