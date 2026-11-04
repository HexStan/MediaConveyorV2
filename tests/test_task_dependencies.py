"""任务依赖：加载期静态校验（白盒）+ run_tasks 编排（灰盒）。

为什么有白盒部分：加载期校验的错误分类（id 非法 / 重复 / 未知引用 /
自引用 / 前向引用）只能从日志与 TaskSpec 归一化结果区分，从任务是否
执行的终态无法观察是哪条规则命中（tests/README.md 原则二）。

为什么驱动 run_tasks 而非 run_task：依赖是"任务间"的编排行为，单个
handler 的 execute() 入口观察不到；run_tasks 同步执行（无停止指令时
不睡眠），可直接在测试线程驱动，任务结果断言落在目录树快照上。
编排任务统一配置 passthrough_patterns = ["*"]：任务"被执行"的终态 =
文件直传进目标目录，无需真实 FFmpeg。
"""

import os

import pytest

from src.app import runtime
from src.app.config import GlobalConfig
from src.app.context import AppContext
from src.core.task_plan import resolve_tasks
from tests.helpers import make_tree, snapshot

# ============================================================
# 白盒：resolve_tasks 加载期校验
# ============================================================


def _task(**extra):
    """最小合法任务字典（解析期不做 I/O，source_dir/dest_dir 路径无需存在）。"""
    task = {
        "source_dir": "s",
        "dest_dir": "d",
        "ffmpeg_cmd": "-i {input} {output}",
        "output_filename": "{basename}.mp4",
    }
    task.update(extra)
    return task


def _plan(tasks, capture_log):
    return resolve_tasks(tasks, logger=capture_log)


class TestIdNormalization:
    def test_missing_id_yields_none_without_warning(self, capture_log):
        specs = _plan([_task()], capture_log)
        assert specs[0].task_id is None
        assert not specs[0].blocked
        assert capture_log.messages == []

    def test_valid_id_registered(self, capture_log):
        specs = _plan([_task(id="stage-1_A")], capture_log)
        assert specs[0].task_id == "stage-1_A"
        assert not specs[0].blocked
        assert capture_log.messages == []

    def test_invalid_chars_warn_and_drop_id(self, capture_log):
        specs = _plan([_task(id="a.b")], capture_log)
        assert specs[0].task_id is None
        assert not specs[0].blocked
        assert capture_log.has("warning", "含非法字符")

    def test_non_string_id_warns_and_drops_id(self, capture_log):
        specs = _plan([_task(id=42)], capture_log)
        assert specs[0].task_id is None
        assert not specs[0].blocked
        assert capture_log.has("warning", "id 配置值非法")

    def test_duplicate_id_errors_and_blocks_later_task(self, capture_log):
        tasks = [_task(id="dup"), _task(id="dup")]
        specs = _plan(tasks, capture_log)
        assert specs[0].task_id == "dup"
        assert specs[1].blocked
        assert capture_log.has("error", "与任务 #1 重复")

    def test_invalid_id_never_collides_with_valid_id(self, capture_log):
        # 格式校验先于唯一性：非法 id 不注册，不触发重复判定
        specs = _plan([_task(id="a.b"), _task(id="a.b")], capture_log)
        assert all(not spec.blocked for spec in specs)
        assert not capture_log.has("error", "重复")
        assert (
            capture_log.messages.count(
                (
                    "warning",
                    "任务 #1 的 id 'a.b' 含非法字符（仅允许 a-z、A-Z、0-9、-、_），视为无 id。",
                )
            )
            == 1
        )


class TestDependsOnNormalization:
    def test_single_string_normalized_to_one_element(self, capture_log):
        specs = _plan([_task(id="a"), _task(depends_on="a")], capture_log)
        assert specs[1].task_id is None
        assert specs[1].depends_on == ("a",)
        assert not specs[1].blocked
        assert capture_log.messages == []

    def test_list_normalized(self, capture_log):
        specs = _plan([_task(id="a"), _task(id="b"), _task(depends_on=["a", "b"])], capture_log)
        assert specs[2].task_id is None
        assert specs[2].depends_on == ("a", "b")
        assert not specs[2].blocked
        assert capture_log.messages == []

    def test_empty_list_no_dependencies(self, capture_log):
        specs = _plan([_task(depends_on=[])], capture_log)
        assert specs[0].depends_on == ()
        assert not specs[0].blocked

    def test_non_string_type_errors_and_blocks(self, capture_log):
        specs = _plan([_task(depends_on=3)], capture_log)
        assert specs[0].blocked
        assert capture_log.has("error", "depends_on 配置值非法")

    def test_non_string_element_errors_and_blocks(self, capture_log):
        specs = _plan([_task(depends_on=["a", 3])], capture_log)
        assert specs[0].blocked
        assert capture_log.has("error", "depends_on 配置值非法")


class TestReferenceValidation:
    def test_backward_reference_kept(self, capture_log):
        tasks = [_task(id="a"), _task(id="b", depends_on=["a"])]
        specs = _plan(tasks, capture_log)
        assert specs[1].task_id == "b"
        assert specs[1].depends_on == ("a",)
        assert not specs[1].blocked
        assert capture_log.messages == []

    def test_unknown_reference_errors_and_blocks(self, capture_log):
        specs = _plan([_task(id="a", depends_on=["ghost"])], capture_log)
        assert specs[0].blocked
        assert capture_log.has("error", "依赖的 id 'ghost' 不存在")

    def test_self_reference_errors_and_blocks(self, capture_log):
        specs = _plan([_task(id="a", depends_on=["a"])], capture_log)
        assert specs[0].blocked
        assert capture_log.has("error", "依赖了自身")

    def test_forward_reference_errors_and_blocks(self, capture_log):
        tasks = [_task(id="a", depends_on=["late"]), _task(id="late")]
        specs = _plan(tasks, capture_log)
        assert specs[0].blocked
        assert specs[1].task_id == "late"
        assert not specs[1].blocked
        assert capture_log.has("error", "位于其后方的任务 'late'")

    def test_duplicate_id_task_still_blocked_when_also_referenced(self, capture_log):
        # 重复 id 未注册，依赖解析到首个出现者；重复任务本身仍被跳过
        tasks = [_task(id="dup"), _task(id="dup"), _task(depends_on=["dup"])]
        specs = _plan(tasks, capture_log)
        assert specs[1].blocked
        assert specs[2].task_id is None
        assert specs[2].depends_on == ("dup",)
        assert not specs[2].blocked

    def test_non_dict_task_blocked(self, capture_log):
        specs = _plan(["oops", _task(id="a")], capture_log)
        assert specs[0].blocked
        assert specs[1].task_id == "a"
        assert not specs[1].blocked


class TestPathValidation:
    """FFmpeg 任务新增的目录位置关系校验（加载期收口）。"""

    def test_dest_inside_source_rejected(self, capture_log):
        specs = _plan([_task(source_dir="s", dest_dir="s/out")], capture_log)
        assert specs[0].blocked
        assert capture_log.has("error", "不得相同或存在嵌套关系")

    def test_source_inside_dest_rejected(self, capture_log, tmp_path):
        specs = _plan(
            [_task(source_dir=str(tmp_path / "dest" / "src"), dest_dir=str(tmp_path / "dest"))],
            capture_log,
        )
        assert specs[0].blocked

    def test_backup_dir_inside_source_rejected(self, capture_log, tmp_path):
        specs = _plan(
            [
                _task(
                    source_dir=str(tmp_path / "src"),
                    dest_dir=str(tmp_path / "dest"),
                    backup_dir=str(tmp_path / "src" / "bak"),
                )
            ],
            capture_log,
        )
        assert specs[0].blocked
        assert capture_log.has("error", "backup_dir")

    def test_remove_source_with_backup_warns_but_keeps_task(self, capture_log, tmp_path):
        specs = _plan(
            [
                _task(
                    source_dir=str(tmp_path / "src"),
                    dest_dir=str(tmp_path / "dest"),
                    backup_dir=str(tmp_path / "bak"),
                    remove_source=True,
                )
            ],
            capture_log,
        )
        assert not specs[0].blocked
        assert capture_log.has("warning", "backup_dir 将被忽略")

    def test_failure_dir_inside_source_rejected(self, capture_log, tmp_path):
        specs = _plan(
            [
                _task(
                    source_dir=str(tmp_path / "src"),
                    dest_dir=str(tmp_path / "dest"),
                    failure_dir=str(tmp_path / "src" / "failed"),
                )
            ],
            capture_log,
        )
        assert specs[0].blocked
        assert capture_log.has("error", "failure_dir")

    def test_failure_dir_inside_dest_rejected(self, capture_log, tmp_path):
        specs = _plan(
            [
                _task(
                    source_dir=str(tmp_path / "src"),
                    dest_dir=str(tmp_path / "dest"),
                    failure_dir=str(tmp_path / "dest" / "failed"),
                )
            ],
            capture_log,
        )
        assert specs[0].blocked
        assert capture_log.has("error", "failure_dir")

    def test_failure_dir_equal_to_backup_dir_rejected(self, capture_log, tmp_path):
        specs = _plan(
            [
                _task(
                    source_dir=str(tmp_path / "src"),
                    dest_dir=str(tmp_path / "dest"),
                    backup_dir=str(tmp_path / "vault"),
                    failure_dir=str(tmp_path / "vault"),
                )
            ],
            capture_log,
        )
        assert specs[0].blocked
        assert capture_log.has("error", "failure_dir 与 backup_dir 不得相同")

    def test_failure_dir_without_failure_count_warns_but_keeps_task(self, capture_log, tmp_path):
        specs = _plan(
            [
                _task(
                    source_dir=str(tmp_path / "src"),
                    dest_dir=str(tmp_path / "dest"),
                    failure_dir=str(tmp_path / "failed"),
                    failure_count=0,
                )
            ],
            capture_log,
        )
        assert not specs[0].blocked
        assert capture_log.has("warning", "failure_dir 将不会被使用")


# ============================================================
# 灰盒：run_tasks 编排（目录树快照 + 日志断言）
# ============================================================


def _passthrough_task(src, dest, **extra):
    task = {
        "source_dir": str(src),
        "dest_dir": str(dest),
        "ffmpeg_cmd": "-i {input} {output}",
        "output_filename": "{basename}.mp4",
        "filter": {"passthrough_patterns": ["*"]},
    }
    task.update(extra)
    os.makedirs(dest, exist_ok=True)
    return task


@pytest.fixture
def run_round(app_context, stub_ffmpeg):
    """驱动一轮 run_tasks：注入任务列表并同步执行（无停止指令）。

    与生产加载路径一致：任务字典先经 resolve_tasks 产出 TaskSpec
    （错误日志在此给出），再写入 AppContext 供 run_tasks 消费。
    """

    def _run(tasks):
        ctx = AppContext.get()
        specs = resolve_tasks(tasks, logger=ctx.logger)
        ctx.update_config(GlobalConfig(), specs)
        runtime.run_tasks()
        return ctx

    return _run


class TestRunTasksOrchestration:
    def test_completed_dependency_runs_dependent(self, tmp_path, run_round):
        first = _passthrough_task(tmp_path / "src-a", tmp_path / "dest-a", id="a")
        second = _passthrough_task(
            tmp_path / "src-b", tmp_path / "dest-b", id="b", depends_on=["a"]
        )
        make_tree(first["source_dir"], {"a.txt": b"alpha"})
        make_tree(second["source_dir"], {"b.txt": b"beta"})

        run_round([first, second])

        assert snapshot(first["dest_dir"]) == {"a.txt": 5}
        assert snapshot(second["dest_dir"]) == {"b.txt": 4}

    def test_failed_dependency_skips_dependent(self, tmp_path, run_round, stub_ffmpeg, capture_log):
        # 前置任务命令模板错误 → 本轮 FAILED（直传任务不经过命令模板，
        # 故前置任务须为真实 FFmpeg 任务才能观察 FAILED）
        first = {
            "source_dir": str(tmp_path / "src-a"),
            "dest_dir": str(tmp_path / "dest-a"),
            "ffmpeg_cmd": "{oops}",
            "output_filename": "{basename}.mp4",
            "id": "a",
        }
        second = _passthrough_task(
            tmp_path / "src-b", tmp_path / "dest-b", id="b", depends_on=["a"]
        )
        make_tree(first["source_dir"], {"a.txt": b"alpha"})
        make_tree(second["source_dir"], {"b.txt": b"beta"})

        run_round([first, second])

        assert capture_log.has("error", "命令模板无效")
        assert capture_log.has("error", "前置任务 a 未完成")
        assert snapshot(second["source_dir"]) == {"b.txt": 4}
        assert snapshot(second["dest_dir"]) == {}

    def test_skipped_dependency_cascades(self, tmp_path, run_round, capture_log):
        # 前置任务缺必填字段 → 装配失败 SKIPPED；依赖链逐级跳过
        first = _passthrough_task(tmp_path / "src-a", tmp_path / "dest-a", id="a")
        del first["source_dir"]
        second = _passthrough_task(
            tmp_path / "src-b", tmp_path / "dest-b", id="b", depends_on=["a"]
        )
        third = _passthrough_task(tmp_path / "src-c", tmp_path / "dest-c", id="c", depends_on=["b"])
        for task, name in ((second, "b.txt"), (third, "c.txt")):
            make_tree(task["source_dir"], {name: name.encode()})

        run_round([first, second, third])

        assert capture_log.has("error", "前置任务 a 未完成")
        assert capture_log.has("error", "前置任务 b 未完成")
        assert snapshot(second["source_dir"]) == {"b.txt": 5}
        assert snapshot(third["source_dir"]) == {"c.txt": 5}

    def test_dependency_on_later_task_blocks_self_only(self, tmp_path, run_round, capture_log):
        first = _passthrough_task(
            tmp_path / "src-a", tmp_path / "dest-a", id="a", depends_on=["late"]
        )
        second = _passthrough_task(tmp_path / "src-b", tmp_path / "dest-b", id="late")
        make_tree(first["source_dir"], {"a.txt": b"alpha"})
        make_tree(second["source_dir"], {"b.txt": b"beta"})

        run_round([first, second])

        assert capture_log.has("error", "位于其后方的任务 'late'")
        assert snapshot(first["source_dir"]) == {"a.txt": 5}
        assert snapshot(second["dest_dir"]) == {"b.txt": 4}

    def test_duplicate_id_skipped_and_dependency_resolves_to_first(
        self, tmp_path, run_round, capture_log
    ):
        first = _passthrough_task(tmp_path / "src-a", tmp_path / "dest-a", id="dup")
        duplicate = _passthrough_task(tmp_path / "src-dup", tmp_path / "dest-dup", id="dup")
        third = _passthrough_task(tmp_path / "src-c", tmp_path / "dest-c", depends_on=["dup"])
        make_tree(first["source_dir"], {"a.txt": b"alpha"})
        make_tree(duplicate["source_dir"], {"dup.txt": b"dup"})
        make_tree(third["source_dir"], {"c.txt": b"charlie"})

        run_round([first, duplicate, third])

        assert capture_log.has("error", "与任务 #1 重复")
        assert snapshot(duplicate["source_dir"]) == {"dup.txt": 3}, "重复 id 的任务应被跳过"
        assert snapshot(first["dest_dir"]) == {"a.txt": 5}
        assert snapshot(third["dest_dir"]) == {"c.txt": 7}, "依赖应解析到首个出现者"

    def test_invalid_id_task_runs_but_cannot_be_depended(self, tmp_path, run_round, capture_log):
        first = _passthrough_task(tmp_path / "src-a", tmp_path / "dest-a", id="a!")
        second = _passthrough_task(tmp_path / "src-b", tmp_path / "dest-b", depends_on=["a!"])
        make_tree(first["source_dir"], {"a.txt": b"alpha"})
        make_tree(second["source_dir"], {"b.txt": b"beta"})

        run_round([first, second])

        assert capture_log.has("warning", "含非法字符")
        assert capture_log.has("error", "依赖的 id 'a!' 不存在")
        assert snapshot(first["dest_dir"]) == {"a.txt": 5}, "非法 id 的任务照常执行"
        assert snapshot(second["source_dir"]) == {"b.txt": 4}

    def test_independent_task_unaffected_by_unmet_dependency(
        self, tmp_path, run_round, stub_ffmpeg, capture_log
    ):
        first = {
            "source_dir": str(tmp_path / "src-a"),
            "dest_dir": str(tmp_path / "dest-a"),
            "ffmpeg_cmd": "{oops}",
            "output_filename": "{basename}.mp4",
            "id": "a",
        }
        second = _passthrough_task(
            tmp_path / "src-b", tmp_path / "dest-b", id="b", depends_on=["a"]
        )
        third = _passthrough_task(tmp_path / "src-c", tmp_path / "dest-c")
        make_tree(first["source_dir"], {"a.txt": b"alpha"})
        make_tree(second["source_dir"], {"b.txt": b"beta"})
        make_tree(third["source_dir"], {"c.txt": b"charlie"})

        run_round([first, second, third])

        assert capture_log.has("error", "前置任务 a 未完成")
        assert snapshot(third["dest_dir"]) == {"c.txt": 7}, "无依赖任务不受影响"
