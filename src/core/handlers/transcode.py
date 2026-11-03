"""transcode 模式处理器：监听源目录，用 FFmpeg 处理音视频文件。

单轮执行的完整流水线（对齐 v1 MediaConveyor 的 scanner + processor 组合）：

1. 扫描：os.walk 确定性排序遍历 → 文件名分类流水线（classify）→
   状态判定（已成功且源文件未变化则跳过；失败达上限永久跳过）→
   修改时间冷却 → 媒体属性过滤（仅在配置了需要 ffprobe 的条件时探测）；
2. 过期清理：延迟删除到期的源文件（删除成功才清记录，失败下轮重试）；
3. 逐文件处理：稳定性检查（stable_duration 的 size 复查，睡眠分片可被
   fast 停止打断）→ FFmpeg 执行或 passthrough 直传 → 源文件善后
   （立即删除 / 延迟删除 / 备份 / 保留）→ 成功记录 → 清理空目录。

单文件失败不中断本轮任务（状态库计数兜底，达到 failure_count 后永久
跳过）；命令模板错误属配置级错误，中止本轮并给出明确报错。
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
from src.core.media.ffmpeg import TemplateFormatError, build_command, run_ffmpeg
from src.core.media.filter import FileFilter
from src.core.media.probe import MediaInfo, empty_media_info, get_media_info
from src.core.registry import register_handler
from src.core.state import SuccessState, file_key, task_key
from src.core.task_config import TranscodeTaskConfig
from src.core.types import FileAction, TranscodeStats
from src.fs import (
    FsOpError,
    PathNotFoundError,
    TransferAbortedError,
    clean_empty_dirs,
    ensure_dir,
    move_file,
)
from src.fs import delete_file as _fs_delete_file
from src.output.presentation import fmt_timespan, print_task_summary

# 稳定性检查睡眠的分片粒度（秒）：分片间响应 fast 停止
_STABLE_SLEEP_SLICE = 0.25


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
    """单轮扫描的产出：待处理条目与延迟删除到期的源文件。"""

    entries: list[_ScanEntry] = field(default_factory=list)
    expired: list[tuple[str, str, str]] = field(default_factory=list)  # (rel, filepath, fkey)


@register_handler("transcode")
class TranscodeHandler(BaseTaskHandler):
    """FFmpeg 转码任务处理器。"""

    # 源目录缺失的告警去重：处理器每轮重建，实例级集合没有记忆，
    # 用类级集合按目录去重（与 v1 Scanner._warned_dirs 语义一致）
    _warned_dirs: ClassVar[set[str]] = set()
    # "继续监听"提示的去重：source_dir → 是否已提示；发现待处理文件即复位
    _monitoring_logged: ClassVar[dict[str, bool]] = {}

    def __init__(self, spec: TaskSpec, now: float | None = None) -> None:
        super().__init__(spec, now)
        # 注册表分派保证（task_config.TASK_MODELS 与 registry 由同一组模式名驱动）
        assert isinstance(self.task, TranscodeTaskConfig), "任务配置模型分派错误"
        self._config: TranscodeTaskConfig = self.task
        self._filter = FileFilter(self._config.filter)
        self._stats = TranscodeStats()
        self._key = task_key(self._config.source_dir, self._config.dest_dir)

    @property
    def stats(self) -> TranscodeStats:
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

            if not report.entries:
                self._log_monitoring_idle(ctx)
            else:
                self._monitoring_logged.pop(config.source_dir, None)
                for entry in report.entries:
                    if ctx.lifecycle.should_stop_before_file():
                        break
                    self._process_entry(ctx, entry)
        except TemplateFormatError as e:
            # 命令模板错误对所有文件必然复现，属配置级错误：
            # 中止本轮并给出明确报错，而不是把失败计数烧在每一个文件上
            ctx.logger.error(f"命令模板无效，本轮任务中止: {e}")
            return False

        print_task_summary(self._stats, time.time() - start_time, ctx.logger)
        return True

    # ---- 扫描 -----------------------------------------------------------

    def _scan(self, ctx: AppContext) -> _ScanReport:
        """遍历源目录，产出待处理条目与延迟删除到期的源文件。"""
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
                    # 已成功处理且源文件未变化：仅判定延迟删除是否到期
                    ctx.logger.debug(f"跳过 {rel_path}，原因: 文件已成功处理过")
                    self._collect_expired(report, config, rel_path, filepath, fkey, success)
                    continue
                if success is not None:
                    # 源文件内容已变化：旧记录（含延迟删除义务）随之失效
                    ctx.logger.info(f"源文件已变化，重新处理: {rel_path}")
                    state.delete_record(self._key, fkey)

                failures = state.get_failure_count(self._key, fkey)
                if failures >= config.failure_count:
                    ctx.logger.debug(
                        f"跳过 {rel_path}，原因: 失败次数 ({failures}) 已达到上限 ({config.failure_count})"
                    )
                    self._stats.record_skipped()
                    continue

                try:
                    stat = os.stat(filepath)
                except OSError as e:
                    ctx.logger.error(f"读取文件失败: {filepath}\n{e}")
                    continue

                if not self._filter.check_mtime(stat.st_mtime, now):
                    ctx.logger.debug(
                        f"跳过 {rel_path}，原因: 文件修改时间未满"
                        f" {config.filter.file_mtime:g} 秒"
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
        config: TranscodeTaskConfig,
        rel_path: str,
        filepath: str,
        fkey: str,
        success: SuccessState,
    ) -> None:
        """延迟删除判定：成功时间距今超过 source_expired_minutes 即到期。"""
        if not config.remove_source or config.source_expired_minutes <= 0:
            return
        if self.now - success.success_time >= config.source_expired_minutes * 60:
            report.expired.append((rel_path, filepath, fkey))

    def _cleanup_expired(self, ctx: AppContext, expired: list[tuple[str, str, str]]) -> None:
        """删除到期的源文件；删除成功才清记录，失败保留记录下轮重试。"""
        for rel_path, filepath, fkey in expired:
            try:
                _fs_delete_file(filepath)
            except PathNotFoundError:
                # 文件已不在（用户手动删除等）：顺手清理失效记录，避免每轮报错
                ctx.require_state().delete_record(self._key, fkey)
                continue
            except FsOpError as e:
                ctx.logger.error(f"删除过期源文件失败: {rel_path}\n{e}")
                continue
            ctx.logger.info(f"已删除过期源文件: {rel_path}")
            ctx.require_state().delete_record(self._key, fkey)
            self._stats.record_expired()

    # ---- 逐文件处理 -----------------------------------------------------

    def _process_entry(self, ctx: AppContext, entry: _ScanEntry) -> None:
        try:
            if not self._check_stable(ctx, entry):
                return
            if entry.action is FileAction.PASSTHROUGH:
                self._passthrough(ctx, entry)
            else:
                self._transcode(ctx, entry)
        except (TransferAbortedError, TemplateFormatError):
            # fast 停止向上传播以中止本轮任务；命令模板错误属配置级错误，
            # 由 _execute 统一处理（不烧失败计数）
            raise
        except Exception as e:  # noqa: BLE001
            # 兜底捕获：单个文件的意外失败不允许中断本轮任务，统一记入失败计数
            ctx.logger.error(f"处理文件失败: {entry.rel_path}\n{e}")
            ctx.require_state().increment_failure(self._key, file_key(entry.filepath))
            self._stats.record_failed()

    def _check_stable(self, ctx: AppContext, entry: _ScanEntry) -> bool:
        """稳定性检查：stable_duration 等待后复查文件大小是否仍在变化。"""
        config = self._config
        filepath, rel_path = entry.filepath, entry.rel_path

        if config.stable_duration <= 0:
            if not os.path.exists(filepath):
                ctx.logger.warning(f"文件 {rel_path} 已不存在，跳过处理。")
                return False
            return True

        try:
            old_size = os.path.getsize(filepath)
        except OSError:
            ctx.logger.warning(f"文件 {rel_path} 已不存在，跳过处理。")
            return False

        ctx.logger.info(f"正在检查 {rel_path} 在 {config.stable_duration:g} 秒内的一致性……")
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
            ctx.logger.warning(f"文件 {rel_path} 已不存在，跳过处理。")
            return False
        if new_size != old_size:
            ctx.logger.info(f"文件 {rel_path} 正在变化，跳过本次处理。")
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
            ctx.logger.info(f"直接移动文件 {entry.rel_path} 至 {config.dest_dir}")
            move_file(entry.filepath, dst_filepath)
        except TransferAbortedError:
            raise
        except Exception as e:  # noqa: BLE001
            ctx.require_state().increment_failure(self._key, file_key(entry.filepath))
            ctx.logger.error(f"直接移动文件失败: {entry.rel_path}\n{e}")
            self._stats.record_failed()
            return

        ctx.require_state().mark_success(
            self._key, file_key(entry.filepath), time.time(), entry.size, entry.mtime
        )
        self._stats.record_passthrough(entry.size)
        clean_empty_dirs(config.source_dir)

    # ---- FFmpeg 转码 -----------------------------------------------------

    def _transcode(self, ctx: AppContext, entry: _ScanEntry) -> None:
        config = self._config
        state = ctx.require_state()
        fkey = file_key(entry.filepath)

        rel_dir = os.path.dirname(entry.rel_path)
        filename = os.path.basename(entry.filepath)
        name = os.path.splitext(filename)[0]
        final_dest_dir = os.path.join(config.dest_dir, rel_dir)
        dst_basepath = os.path.join(final_dest_dir, name)

        use_fallback = (
            config.fallback_count > 0
            and state.get_ffmpeg_failure_count(self._key, fkey) >= config.fallback_count
        )
        template = config.ffmpeg_cmd_fallback if use_fallback else config.ffmpeg_cmd
        duration_note = self._duration_note(entry)
        if use_fallback:
            ctx.logger.info(f"使用备用命令处理 {entry.rel_path}{duration_note}。")
        else:
            ctx.logger.info(f"开始处理 {entry.rel_path}{duration_note}。")

        cmd = build_command(template, entry.filepath, dst_basepath)
        ctx.logger.debug(f"执行 FFmpeg 命令: {cmd}")

        ensure_dir(final_dest_dir)
        # 记录处理前目标目录的文件列表，用于失败时清理不完整的输出文件
        existing_files = set(os.listdir(final_dest_dir))

        ok = run_ffmpeg(
            cmd,
            rel_path=entry.rel_path,
            logger=ctx.logger,
            coordinator=ctx.lifecycle,
            timeout=config.ffmpeg_timeout,
        )
        if ok:
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
        self._cleanup_partial_output(final_dest_dir, existing_files, name)
        self._stats.record_failed()

    def _duration_note(self, entry: _ScanEntry) -> str:
        """日志中的媒体时长注记；未探测到时长时省略（不做二次 ffprobe）。"""
        if entry.media_info is None:
            return ""
        duration = entry.media_info["duration"]
        if duration <= 0:
            return ""
        return f"，媒体时长 {fmt_timespan(duration)}"

    def _handle_success(
        self, ctx: AppContext, entry: _ScanEntry, rel_dir: str, filename: str
    ) -> None:
        """转码成功后的源文件善后：立即删除 / 延迟删除 / 备份 / 保留。"""
        config = self._config
        state = ctx.require_state()
        fkey = file_key(entry.filepath)

        if config.remove_source:
            if config.source_expired_minutes == 0:
                try:
                    _fs_delete_file(entry.filepath)
                    ctx.logger.info(f"已删除源文件: {entry.rel_path}")
                except FsOpError as e:
                    ctx.logger.error(f"删除源文件失败: {entry.rel_path}\n{e}")
                state.mark_success(self._key, fkey, time.time(), entry.size, entry.mtime)
            else:
                state.mark_success(self._key, fkey, time.time(), entry.size, entry.mtime)
                ctx.logger.info(
                    f"源文件 {entry.rel_path} 将在 {config.source_expired_minutes:g} 分钟后删除。"
                )
        else:
            if config.backup_dir:
                bak_dir = os.path.join(config.backup_dir, rel_dir)
                bak_filepath = os.path.join(bak_dir, filename)
                try:
                    ensure_dir(bak_dir)
                    ctx.logger.debug(
                        f"移动源文件到备份目录: {entry.filepath} -> {bak_filepath}"
                    )
                    move_file(entry.filepath, bak_filepath)
                except TransferAbortedError:
                    raise
                except Exception as e:  # noqa: BLE001
                    ctx.logger.error(f"移动源文件到备份目录失败: {entry.rel_path}\n{e}")
                    state.increment_failure(self._key, fkey)
                    self._stats.record_failed()
                    return
            else:
                ctx.logger.info(f"源文件保留在原位置: {entry.rel_path}")
            state.mark_success(self._key, fkey, time.time(), entry.size, entry.mtime)

        self._stats.record_processed(entry.size)
        clean_empty_dirs(config.source_dir)

    def _cleanup_partial_output(
        self, final_dest_dir: str, existing_files: set[str], name: str
    ) -> None:
        """删除失败转码留下的不完整输出（以输出基础名开头的新增文件）。"""
        if not os.path.isdir(final_dest_dir):
            return
        new_files = set(os.listdir(final_dest_dir)) - existing_files
        for filename in new_files:
            if not filename.startswith(name):
                continue
            path = os.path.join(final_dest_dir, filename)
            if os.path.isfile(path):
                with contextlib.suppress(OSError):
                    os.remove(path)

    # ---- 提示去重 ---------------------------------------------------------

    def _warn_missing_source(self, ctx: AppContext) -> None:
        source = self._config.source_dir
        if source not in TranscodeHandler._warned_dirs:
            TranscodeHandler._warned_dirs.add(source)
            ctx.logger.warning(f"源目录不存在: {source}")

    def _log_monitoring_idle(self, ctx: AppContext) -> None:
        source = self._config.source_dir
        if not TranscodeHandler._monitoring_logged.get(source):
            TranscodeHandler._monitoring_logged[source] = True
            ctx.logger.info(f"未发现待处理文件，继续监听 {source}。")
