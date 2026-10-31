"""ISS-003 单实例锁测试：同目录互斥、不同目录互不干扰、释放后可重新获取。

全部在 tmp_path 下运行，不接触真实 data/ 目录，不启动游戏控制。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from src.common.single_instance import SingleInstanceLock


def test_acquire_writes_lock_file_and_holder(tmp_path: Path) -> None:
    lock = SingleInstanceLock(tmp_path)
    try:
        assert lock.acquire() is True
        assert lock.held is True
        assert lock.lock_path == tmp_path / "wows_script.lock"
        assert lock.lock_path.is_file()
        payload = json.loads(lock.lock_path.read_text(encoding="utf-8"))
        assert payload["pid"] == os.getpid()
        assert "started_at" in payload
        assert "cwd" in payload
    finally:
        lock.release()
    assert not lock.lock_path.exists()


def test_second_instance_is_rejected_and_can_read_holder(tmp_path: Path) -> None:
    first = SingleInstanceLock(tmp_path)
    second = SingleInstanceLock(tmp_path)
    try:
        assert first.acquire() is True
        assert second.acquire() is False
        assert second.held is False
        holder = second.holder
        assert holder is not None
        assert holder["pid"] == os.getpid()
    finally:
        second.release()
        first.release()


def test_release_allows_third_instance(tmp_path: Path) -> None:
    first = SingleInstanceLock(tmp_path)
    assert first.acquire() is True
    first.release()
    assert not first.lock_path.exists()

    third = SingleInstanceLock(tmp_path)
    try:
        assert third.acquire() is True
    finally:
        third.release()


def test_different_data_dirs_do_not_conflict(tmp_path: Path) -> None:
    lock_a = SingleInstanceLock(tmp_path / "a")
    lock_b = SingleInstanceLock(tmp_path / "b")
    try:
        assert lock_a.acquire() is True
        assert lock_b.acquire() is True
    finally:
        lock_a.release()
        lock_b.release()


def test_acquire_is_idempotent(tmp_path: Path) -> None:
    lock = SingleInstanceLock(tmp_path)
    try:
        assert lock.acquire() is True
        assert lock.acquire() is True
        assert lock.held is True
    finally:
        lock.release()


def test_release_is_idempotent_and_context_manager(tmp_path: Path) -> None:
    lock = SingleInstanceLock(tmp_path)
    lock.release()  # 未持有时调用不应抛异常
    lock.release()
    with SingleInstanceLock(tmp_path) as held:
        assert held.held is True
    assert not lock.lock_path.exists()


def test_context_manager_raises_when_already_held(tmp_path: Path) -> None:
    first = SingleInstanceLock(tmp_path)
    with first, pytest.raises(RuntimeError), SingleInstanceLock(tmp_path):
        pass
