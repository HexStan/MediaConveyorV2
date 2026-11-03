"""传输中止测试（stop --fast 的文件系统侧契约）。

中止检查钩子是 fs.abort 提供的注入缝（生产环境由 LifecycleCoordinator
注入），测试借助它在 chunk 边界精确触发中止，从外部断言三个不变量：
最终路径不受影响、已写入的临时文件被清理、异常不被改写成操作失败。
"""

import os

import pytest

from src import fs
from src.fs import TransferAbortedError, atomic_writer, copy_file


@pytest.fixture
def abort_after():
    """在第 n 次 chunk 边界检查时触发中止的钩子（测试后复位）。"""
    state = {"calls": 0, "fire_on": 0}

    def check():
        state["calls"] += 1
        return state["fire_on"] > 0 and state["calls"] >= state["fire_on"]

    fs.set_abort_check(check)
    yield state
    fs.set_abort_check(None)


def test_copy_aborts_before_first_chunk(tmp_path, abort_after):
    src = tmp_path / "src.bin"
    src.write_bytes(b"data")
    dest = tmp_path / "dest" / "src.bin"

    abort_after["fire_on"] = 1

    with pytest.raises(TransferAbortedError):
        copy_file(str(src), str(dest))

    assert not dest.exists()
    assert not _partial_files(dest.parent)


def test_copy_aborts_mid_file_leaves_no_residue(tmp_path, abort_after):
    """大文件复制到第 3 次边界检查时中止：已写入的临时文件被清理。"""
    src = tmp_path / "big.bin"
    src.write_bytes(os.urandom(1024 * 1024 + 7))  # 超过 1 个 chunk
    dest = tmp_path / "dest" / "big.bin"

    abort_after["fire_on"] = 3

    with pytest.raises(TransferAbortedError):
        copy_file(str(src), str(dest))

    assert not dest.exists()
    assert not _partial_files(dest.parent)
    assert src.stat().st_size == 1024 * 1024 + 7  # 源文件完好


def test_copy_without_abort_completes(tmp_path, abort_after):
    """钩子在位但始终返回 False 时行为与普通复制一致。"""
    src = tmp_path / "ok.bin"
    src.write_bytes(b"payload")
    dest = tmp_path / "dest" / "ok.bin"

    copy_file(str(src), str(dest))

    assert dest.read_bytes() == b"payload"


def test_aborted_error_not_wrapped_as_fs_error(tmp_path, abort_after):
    """中止异常必须原样穿透，不得被改写成 FsOpError。"""
    src = tmp_path / "x.bin"
    src.write_bytes(b"data")

    abort_after["fire_on"] = 1

    with pytest.raises(TransferAbortedError) as exc_info:
        copy_file(str(src), str(tmp_path / "dest" / "x.bin"))

    assert not isinstance(exc_info.value, fs.FsOpError)


def test_atomic_writer_cleans_temp_on_abort_inside_block(tmp_path):
    """atomic_writer 块内抛出中止异常时清理临时文件（服务端接收路径）。"""
    dest = tmp_path / "upload.bin"

    with pytest.raises(TransferAbortedError), atomic_writer(str(dest)) as f:
        f.write(b"half")
        raise TransferAbortedError("接收被停止指令中止")

    assert not dest.exists()
    assert not _partial_files(tmp_path)


def _partial_files(directory):
    if not os.path.isdir(directory):
        return []
    return [n for n in os.listdir(directory) if n.endswith(".mc-part")]
