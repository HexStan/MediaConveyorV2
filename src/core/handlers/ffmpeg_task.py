"""FFmpeg 任务处理器：监听源目录，用 FFmpeg 处理音视频文件。

单轮执行的完整流水线（对齐 v1 MediaConveyor 的 scanner + processor 组合）：

1. 扫描：os.walk 确定性排序遍历 → 文件名分类流水线（classify）→
   状态判定（已成功且源文件未变化则跳过；失败达上限的文件进入失败
   隔离或永久跳过，failure_count = 0 表示永不因失败跳过）→
   修改时间冷却 → 媒体属性过滤（仅在配置了需要 ffprobe 的条件时探测）；
2. 善后清理：remove_source 模式下到期的源文件（source_expired_minutes
   = 0 的立即删除模式，记录存在即已到期；删除成功才清记录，失败下轮
   重试）与已达失败上限的源文件（配置 failure_dir 时移入该目录并清
   记录，移动失败下轮重试；未配置时永久跳过、留在源目录）；
3. 逐文件处理：稳定性检查（stable_duration 的 size 复查，睡眠分片可被
   fast 停止打断）→ FFmpeg 写入瞬态 part 文件（.{output}.mc-ffmpeg-part，
   成功后原子改名复原，失败与 fast 停止均清理）或 passthrough 直传 →
   源文件善后（立即删除 / 延迟删除 / 备份 / 保留），状态记录仅在义务
   存续时保留 → 清理空目录。

单文件失败不中断本轮任务（状态库计数兜底，达到 failure_count 后永久
跳过，配置 failure_dir 时改为移入隔离目录；0 表示永不因失败跳过）；
命令模板错误属配置级错误，中止本轮并给出明确报错。
"""

import contextlib
import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from src.core.task_plan import TaskSpec

from src.app.context import AppContext
from src.core.handlers.base import BaseTaskHandler
from src.core.media.ffmpeg.command import (
    TemplateFormatError,
    build_command,
    part_output_filename,
    render_output_filename,
)
from src.core.media.ffmpeg.execute import run_ffmpeg
from src.core.media.filter import FileFilter
from src.core.media.probe import MediaInfo, empty_media_info, get_media_info
from src.core.state import SuccessState, file_key, task_key
from src.core.task_config import TaskConfig
from src.core.types import FileAction, TaskRunStats
from src.fs import (
    FsOpError,
    PathNotFoundError,
    TransferAbortedError,
    clean_empty_dirs,
    ensure_dir,
    move_file,
)
from src.fs import delete_file as _fs_delete_file
from src.output.presentation import print_task_summary

# 稳定性检查睡眠的分片粒度（秒）：分片间响应 fast 停止
_STABLE_SLEEP_SLICE = 0.5


@dataclass
class _ScanEntry:
    """单个待处理文件的扫描结果。"""

    filepath: str
    rel_path: str
    action: FileAction
    size: int
    mtime: float
    media_info: MediaInfo | None = None


@dataclass
class _ScanReport:
    """单轮扫描的产出：待处理条目与待清理的源文件（待删除 / 待隔离）。"""

    entries: list[_ScanEntry] = field(default_factory=list)
    expired: list[tuple[str, str, str]] = field(default_factory=list)  # (rel, filepath, fkey)
    evictions: list[tuple[str, str, str]] = field(default_factory=list)  # (rel, filepath, fkey)


def _discard_part_file(part_path: str) -> None:
    """尽力删除 FFmpeg 输出的 part 文件（本轮失败残骸或上次崩溃残留）。"""
    with contextlib.suppress(OSError):
        os.remove(part_path)


class FFmpegTaskHandler(BaseTaskHandler):
    """FFmpeg 任务处理器。"""

    # 源目录缺失的告警去重：处理器每轮重建，实例级集合没有记忆，
    # 用类级集合按目录去重（与 v1 Scanner._warned_dirs 语义一致）
    _warned_dirs: ClassVar[set[str]] = set()

    def __init__(self, spec: TaskSpec, now: float | None = None) -> None:
        super().__init__(spec, now)
        self._config: TaskConfig = self.task
        self._filter = FileFilter(self._config.filter)
        self._stats = TaskRunStats()
        self._key = task_key(self._config.source_dir, self._config.dest_dir)

    @property
    def stats(self) -> TaskRunStats:
        """本轮执行的统计（灰盒测试断言用）。"""
        return self._stats

    # ---- 模板方法入口 ---------------------------------------------------

    def _execute(self) -> bool:
        ctx = AppContext.get()
        config = self._config

        if not os.path.isdir(config.source_dir):
            self._warn_missing_source(ctx)
            return True

        start_time = time.time()
        try:
            report = self._scan(ctx)
            self._cleanup_expired(ctx, report.expired)
            self._evict_failed(ctx, report.evictions)

            total = len(report.entries)
            for index, entry in enumerate(report.entries, start=1):
                if ctx.lifecycle.should_stop_before_file():
                    break
                self._handle_entry(ctx, entry, index, total)
        except TemplateFormatError as e:
            # 命令模板错误对所有文件必然复现，属配置级错误：
            # 中止本轮并给出明确报错，而不是把失败计数烧在每一个文件上
            ctx.logger.error(f"命令模板无效，本轮任务中止: {e}")
            return False

        print_task_summary(self._stats, time.time() - start_time, ctx.logger)
        return True

    # ---- 扫描 -----------------------------------------------------------

    def _scan(self, ctx: AppContext) -> _ScanReport:
        """遍历源目录，产出待处理条目与待删除到期的源文件。"""
        config = self._config
        state = ctx.require_state()
        now = self.now
        report = _ScanReport()

        for root, dirs, files in os.walk(config.source_dir):
            # 排序保证跨平台遍历顺序确定（处理顺序与日志可复现）
            dirs.sort()
            files.sort()
            for name in files:
                filepath = os.path.join(root, name)
                rel_path = os.path.relpath(filepath, config.source_dir)

                action = self._filter.classify(rel_path)
                if action is FileAction.SKIP:
                    ctx.logger.debug(f"跳过 {rel_path}，原因: 文件名不匹配过滤规则")
                    continue

                fkey = file_key(filepath)
                success = state.get_success_state(self._key, fkey)
                if success is not None and not self._source_changed(filepath, success):
                    # 已成功处理且源文件未变化：仅判定待删除义务是否到期
                    ctx.logger.debug(f"跳过 {rel_path}，原因: 文件已成功处理过")
                    self._collect_expired(report, config, rel_path, filepath, fkey, success)
                    continue
                if success is not None:
                    # 源文件内容已变化：旧记录承载的义务随之失效
                    ctx.logger.debug(f"源文件已变化，重新处理: {rel_path}")
                    state.delete_record(self._key, fkey)

                # failure_count = 0 表示永不因失败跳过，失败文件每轮重试；
                # 达到上限的文件：配置 failure_dir 时收集进隔离清单（移动
                # 由 _evict_failed 执行，失败下轮重试），否则永久跳过
                failures = state.get_failure_count(self._key, fkey)
                if config.failure_count > 0 and failures >= config.failure_count:
                    if config.failure_dir:
                        report.evictions.append((rel_path, filepath, fkey))
                    else:
                        ctx.logger.debug(
                            f"跳过 {rel_path}，原因: 失败次数 ({failures}) 已达到上限 ({config.failure_count})"
                        )
                        self._stats.record_skipped()
                    continue

                try:
                    stat = os.stat(filepath)
                except OSError as e:
                    ctx.logger.error(f"读取 {filepath} 的元数据失败:\n{e}")
                    continue

                if not self._filter.check_mtime(stat.st_mtime, now):
                    ctx.logger.debug(
                        f"跳过 {rel_path}，原因: 文件修改时间未满 {config.filter.file_mtime:g} 秒"
                    )
                    continue

                media_info: MediaInfo | None = None
                if action is not FileAction.PASSTHROUGH:
                    if self._filter.requires_media_info():
                        ctx.logger.debug(f"正在获取媒体信息: {rel_path}")
                        media_info = get_media_info(filepath, ctx.logger)
                        media_info["size"] = stat.st_size
                    else:
                        # 不触发 ffprobe：仅 size 维度参与匹配
                        media_info = empty_media_info()
                        media_info["size"] = stat.st_size
                    if not self._filter.match(media_info, ctx.logger, rel_path):
                        continue

                report.entries.append(
                    _ScanEntry(
                        filepath=filepath,
                        rel_path=rel_path,
                        action=action,
                        size=stat.st_size,
                        mtime=stat.st_mtime,
                        media_info=media_info,
                    )
                )
                ctx.logger.info(f"监测到待处理文件: {rel_path}")

        return report

    def _source_changed(self, filepath: str, success: SuccessState) -> bool:
        """源文件内容是否已变化（size/mtime 与成功记录不再吻合）。"""
        try:
            stat = os.stat(filepath)
        except OSError:
            # 文件已消失：不存在"变化"，仅走延迟删除判定
            return False
        return stat.st_size != success.source_size or stat.st_mtime != success.source_mtime

    def _collect_expired(
        self,
        report: _ScanReport,
        config: TaskConfig,
        rel_path: str,
        filepath: str,
        fkey: str,
        success: SuccessState,
    ) -> None:
        """待删除义务的到期判定：成功时间距今超过 source_expired_minutes 即到期。

        source_expired_minutes = 0 的立即删除模式，记录存在即已到期——
        删除失败后保留的成功记录由此在下轮重试删除。
        """
        if not config.remove_source:
            return
        if self.now - success.success_time >= config.source_expired_minutes * 60:
            report.expired.append((rel_path, filepath, fkey))

    def _cleanup_expired(self, ctx: AppContext, expired: list[tuple[str, str, str]]) -> None:
        """删除到期的源文件；删除成功（义务履行）才清记录，失败保留记录下轮重试。"""
        for rel_path, filepath, fkey in expired:
            try:
                _fs_delete_file(filepath)
            except PathNotFoundError:
                # 文件已不在（用户手动删除等）：顺手清理失效记录，避免每轮报错
                ctx.require_state().delete_record(self._key, fkey)
                continue
            except FsOpError as e:
                ctx.logger.error(f"删除源文件失败: {rel_path}\n{e}")
                continue
            ctx.logger.info(f"已删除源文件: {rel_path}")
            ctx.require_state().delete_record(self._key, fkey)
            self._stats.record_expired()

    def _evict_failed(self, ctx: AppContext, evictions: list[tuple[str, str, str]]) -> None:
        """把已达失败上限的源文件移入 failure_dir（保持子目录结构）。

        移动成功即义务了结：源文件离开源树，失败记录随之清除（用户之后
        放回的同名文件从头处理）；移动失败保留记录与原文件，下轮重试；
        文件已消失（用户手动移走等）时顺手清理失效记录。
        """
        config = self._config
        moved = False
        for rel_path, filepath, fkey in evictions:
            if not os.path.exists(filepath):
                # 文件已不在（用户手动移走/删除等）：顺手清理失效记录，避免每轮报错
                ctx.require_state().delete_record(self._key, fkey)
                continue
            dst_dir = os.path.join(config.failure_dir, os.path.dirname(rel_path))
            dst_filepath = os.path.join(dst_dir, os.path.basename(filepath))
            try:
                move_file(filepath, dst_filepath)
            except TransferAbortedError:
                raise
            except Exception as e:  # noqa: BLE001
                ctx.logger.error(f"移入失败目录失败: {rel_path}\n{e}")
                continue
            ctx.logger.info(f"{rel_path} 的失败次数达到上限，已移入失败目录。")
            ctx.require_state().delete_record(self._key, fkey)
            self._stats.record_evicted()
            moved = True

        if moved:
            clean_empty_dirs(config.source_dir)

    # ---- 逐文件处理 -----------------------------------------------------

    def _handle_entry(self, ctx: AppContext, entry: _ScanEntry, index: int, total: int) -> None:
        # 流转路线的起点标记：此后的整条路线（稳定性检查 → 处理 → 善后）
        # 都不再重复输出文件路径
        ctx.logger.info(f"【{index}/{total}】准备处理: {entry.rel_path}")
        try:
            if not self._check_stable(ctx, entry):
                return
            if entry.action is FileAction.PASSTHROUGH:
                self._passthrough(ctx, entry)
            else:
                self._process(ctx, entry)
        except TransferAbortedError, TemplateFormatError:
            # fast 停止向上传播以中止本轮任务；命令模板错误属配置级错误，
            # 由 _execute 统一处理（不烧失败计数）
            raise
        except Exception as e:  # noqa: BLE001
            # 兜底捕获：单个文件的意外失败不允许中断本轮任务，统一记入失败计数
            ctx.logger.error(f"处理文件时发生未知失败:\n{e}")
            ctx.require_state().increment_failure(self._key, file_key(entry.filepath))
            self._stats.record_failed()

    def _check_stable(self, ctx: AppContext, entry: _ScanEntry) -> bool:
        """稳定性检查：stable_duration 等待后复查文件大小是否仍在变化。"""
        config = self._config
        filepath = entry.filepath

        if config.stable_duration <= 0:
            if not os.path.exists(filepath):
                ctx.logger.warning("文件已不存在，跳过处理。")
                return False
            return True

        try:
            old_size = os.path.getsize(filepath)
        except OSError:
            ctx.logger.warning("文件已不存在，跳过处理。")
            return False

        ctx.logger.info(f"正在检查文件在 {config.stable_duration:g} 秒内的一致性……")
        remaining = config.stable_duration
        while remaining > 0:
            # 分片睡眠：fast 停止立即中止，不再阻塞整个监听循环
            if ctx.lifecycle.is_aborted():
                raise TransferAbortedError()
            chunk = min(remaining, _STABLE_SLEEP_SLICE)
            time.sleep(chunk)
            remaining -= chunk

        try:
            new_size = os.path.getsize(filepath)
        except OSError:
            ctx.logger.warning("文件已不存在，跳过处理。")
            return False
        if new_size != old_size:
            ctx.logger.info("文件正在变化，跳过本次处理。")
            return False
        return True

    # ---- passthrough 直传 -----------------------------------------------

    def _passthrough(self, ctx: AppContext, entry: _ScanEntry) -> None:
        """不经 FFmpeg 直接移动到目标目录（保持子目录结构）。"""
        config = self._config
        final_dest_dir = os.path.join(config.dest_dir, os.path.dirname(entry.rel_path))
        dst_filepath = os.path.join(final_dest_dir, os.path.basename(entry.filepath))

        try:
            ensure_dir(final_dest_dir)
            ctx.logger.info("正在直传文件……")
            move_file(entry.filepath, dst_filepath)
        except TransferAbortedError:
            raise
        except Exception as e:  # noqa: BLE001
            ctx.require_state().increment_failure(self._key, file_key(entry.filepath))
            ctx.logger.error(f"直传文件失败:\n{e}")
            self._stats.record_failed()
            return

        # 直传成功：源文件已移至目标目录、离开源树，义务了结不留记录
        # （并顺带清掉此前失败尝试残留的计数）
        ctx.require_state().delete_record(self._key, file_key(entry.filepath))
        self._stats.record_passthrough(entry.size)
        clean_empty_dirs(config.source_dir)

    # ---- FFmpeg 处理 -----------------------------------------------------

    def _process(self, ctx: AppContext, entry: _ScanEntry) -> None:
        config = self._config
        state = ctx.require_state()
        fkey = file_key(entry.filepath)

        rel_dir = os.path.dirname(entry.rel_path)
        filename = os.path.basename(entry.filepath)
        name = os.path.splitext(filename)[0]
        final_dest_dir = os.path.join(config.dest_dir, rel_dir)
        out_filename = render_output_filename(config.output_filename, name)
        dst_output_path = os.path.join(final_dest_dir, out_filename)
        part_output_path = os.path.join(final_dest_dir, part_output_filename(out_filename))

        use_fallback = (
            config.fallback_count > 0
            and state.get_ffmpeg_failure_count(self._key, fkey) >= config.fallback_count
        )
        template = config.ffmpeg_cmd_fallback if use_fallback else config.ffmpeg_cmd
        if use_fallback:
            ctx.logger.info("正在切换备用参数来调用 FFmpeg 处理文件……")
        else:
            ctx.logger.info("正在调用 FFmpeg 处理文件……")

        cmd = build_command(template, entry.filepath, part_output_path)
        ctx.logger.debug(f"执行 FFmpeg 命令: {cmd}")

        # get_media_info 阶段已探测到有效时长（>0）时直接用作状态行总时长；
        # 未探测或探测无时长时传 None，由 run_ffmpeg 从 stderr 启动横幅兜底嗅探
        probe_duration = entry.media_info["duration"] if entry.media_info else 0.0

        ensure_dir(final_dest_dir)
        # 先清理上次崩溃残留的同名 part 文件（对齐 fs/atomic 的先清后建），
        # 此后 part 文件必为本轮产物，失败清理无需比对既有文件
        _discard_part_file(part_output_path)

        try:
            ok = run_ffmpeg(
                cmd,
                logger=ctx.logger,
                coordinator=ctx.lifecycle,
                timeout=config.ffmpeg_timeout,
                total_duration=probe_duration if probe_duration > 0 else None,
            )
        except TransferAbortedError:
            # fast 停止：写入中的 part 文件必不完整，尽力清理后向上传播
            _discard_part_file(part_output_path)
            raise

        if ok:
            if self._restore_output_name(ctx, entry, part_output_path, dst_output_path, fkey):
                self._handle_success(ctx, entry, rel_dir, filename)
            return

        # 失败计数：主命令失败专计（供 fallback 切换），fallback 阶段的失败
        # 计入普通失败；未配置 fallback 时同样计入普通失败
        if use_fallback:
            state.increment_failure(self._key, fkey)
        elif config.fallback_count > 0:
            state.increment_ffmpeg_failure(self._key, fkey)
        else:
            state.increment_failure(self._key, fkey)
        _discard_part_file(part_output_path)
        self._stats.record_failed()

    def _restore_output_name(
        self,
        ctx: AppContext,
        entry: _ScanEntry,
        part_path: str,
        dst_path: str,
        fkey: str,
    ) -> bool:
        """处理成功后把 part 文件原子改名为最终输出文件名。

        part 与最终输出同目录，move_file（os.replace）为同文件系统原子
        操作：观察目标目录的消费者只会看到完整的最终文件，已存在的旧
        最终文件被整体顶替。改名失败（如最终文件被其他程序占用）按
        普通文件失败处理——不烧 fallback 专用计数（换备用命令无济于
        事），part 文件随之清理，下轮重试整轮编码。
        """
        try:
            move_file(part_path, dst_path)
        except FsOpError as e:
            ctx.logger.error(f"写入输出文件失败:\n{e}")
            ctx.require_state().increment_failure(self._key, fkey)
            _discard_part_file(part_path)
            self._stats.record_failed()
            return False
        ctx.logger.debug(f"输出文件已就位: {dst_path}")
        return True

    def _handle_success(
        self, ctx: AppContext, entry: _ScanEntry, rel_dir: str, filename: str
    ) -> None:
        """FFmpeg 处理成功后的源文件善后：立即删除 / 延迟删除 / 备份 / 保留。

        状态记录仅在义务存续时保留：源文件离开源树（立即删除、备份成功）
        即义务了结，不留记录；源文件仍在源树（延迟删除窗口、保留在原地、
        删除失败待重试）才写成功记录，承载待删除 / 防重复义务。
        """
        config = self._config
        state = ctx.require_state()
        fkey = file_key(entry.filepath)

        if config.remove_source:
            if config.source_expired_minutes == 0:
                try:
                    _fs_delete_file(entry.filepath)
                except FsOpError as e:
                    # 删除义务未履行：写成功记录（待删除凭证），下轮经
                    # 到期清理路径重试（立即删除模式记录存在即已到期）
                    ctx.logger.error(f"删除源文件失败:\n{e}")
                    state.mark_success(self._key, fkey, time.time(), entry.size, entry.mtime)
                else:
                    ctx.logger.info("已删除源文件。")
                    # 义务已了结：源文件离开源树，不留记录（并清残留失败计数）
                    state.delete_record(self._key, fkey)
            else:
                # 延迟删除窗口：记录承载待删除义务，兼作窗口期防重复依据
                state.mark_success(self._key, fkey, time.time(), entry.size, entry.mtime)
                ctx.logger.info(f"源文件将在 {config.source_expired_minutes:g} 分钟后删除。")
        else:
            if config.backup_dir:
                bak_dir = os.path.join(config.backup_dir, rel_dir)
                bak_filepath = os.path.join(bak_dir, filename)
                try:
                    ensure_dir(bak_dir)
                    ctx.logger.debug(f"移动源文件到备份目录: {entry.filepath} -> {bak_filepath}")
                    move_file(entry.filepath, bak_filepath)
                except TransferAbortedError:
                    raise
                except Exception as e:  # noqa: BLE001
                    ctx.logger.error(f"源文件备份失败:\n{e}")
                    state.increment_failure(self._key, fkey)
                    self._stats.record_failed()
                    return
                # 备份成功：源文件离开源树，义务了结不留记录（并清残留失败计数）
                state.delete_record(self._key, fkey)
            else:
                ctx.logger.debug(f"源文件保留在原位置: {entry.rel_path}")
                # 保留在原地：记录承载防重复义务（内容变化即失效重处理）
                state.mark_success(self._key, fkey, time.time(), entry.size, entry.mtime)

        self._stats.record_processed(entry.size)
        clean_empty_dirs(config.source_dir)

    # ---- 提示去重 ---------------------------------------------------------

    def _warn_missing_source(self, ctx: AppContext) -> None:
        source = self._config.source_dir
        if source not in FFmpegTaskHandler._warned_dirs:
            FFmpegTaskHandler._warned_dirs.add(source)
            ctx.logger.warning(f"源目录不存在: {source}")
