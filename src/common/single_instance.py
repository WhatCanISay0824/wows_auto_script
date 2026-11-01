"""单实例锁（ISS-003，实机问题记录：残留进程导致双实例）。

控制游戏输入的程序在同一时间只允许一个实例运行，否则两个实例会同时发送键鼠，
行为不可预测且调试结论被污染。实现口径：

- **权威锁**：Windows 命名互斥量（``Local\\wows_script_<目录哈希>``）。
  互斥量随进程消亡自动释放，崩溃后不会留下“假占用”；已有实例时
  :meth:`SingleInstanceLock.acquire` 返回 False，并尽量读出锁文件里的持有者信息。
- **诊断锁文件**：``<data_dir>/wows_script.lock``，只记录 PID / 启动时间 / 工作目录，
  便于人工排查，不含账号、密码、令牌等任何凭据。
- **非 Windows**：使用 ``O_CREAT | O_EXCL`` 锁文件兜底（项目实际只支持 Windows，
  该分支仅用于离线测试/工具）。
- 释放顺序：先删锁文件再关闭互斥量；``atexit`` 兜底释放。
"""

from __future__ import annotations

import atexit
import contextlib
import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:  # pragma: no cover - 平台相关
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    _kernel32.CreateMutexW.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _ERROR_ALREADY_EXISTS = 183
else:  # pragma: no cover
    _kernel32 = None
    _ERROR_ALREADY_EXISTS = 183


class SingleInstanceLock:
    """同一 data 目录只允许一个进程持有的运行锁。

    用法::

        lock = SingleInstanceLock(data_dir)
        if not lock.acquire():
            print("已有实例在运行", lock.holder)
            raise SystemExit(3)
        try:
            ...  # 运行会话
        finally:
            lock.release()
    """

    def __init__(self, data_dir: Path | str, *, name: str = "wows_script") -> None:
        self.data_dir = Path(data_dir)
        self._name = name
        self._mutex: Any | None = None
        self._held = False
        self._lock_path = self.data_dir / f"{name}.lock"
        self._holder: dict[str, Any] | None = None
        self._atexit_registered = False

    # ------------------------------------------------------------------
    @property
    def held(self) -> bool:
        return self._held

    @property
    def lock_path(self) -> Path:
        return self._lock_path

    @property
    def holder(self) -> dict[str, Any] | None:
        """已有实例的锁文件信息（PID/启动时间/工作目录），读不到时为 None。"""
        return self._holder if self._holder is not None else self._read_lock_file()

    # ------------------------------------------------------------------
    def acquire(self) -> bool:
        """尝试获取锁；成功返回 True（幂等），已被其它实例持有返回 False。"""
        if self._held:
            return True
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self._acquire_mutex():
            self._holder = self._read_lock_file()
            logger.warning(
                "已存在另一个脚本实例（PID=%s，启动于 %s），本次不启动游戏控制",
                (self._holder or {}).get("pid", "unknown"),
                (self._holder or {}).get("started_at", "unknown"),
            )
            return False
        self._held = True
        self._write_lock_file()
        if not self._atexit_registered:
            atexit.register(self.release)
            self._atexit_registered = True
        return True

    def release(self) -> None:
        """释放锁（幂等）；正常退出、异常退出、Ctrl+C 都应调用。"""
        if not self._held and self._mutex is None:
            with contextlib.suppress(OSError):
                if self._lock_path.exists():
                    self._lock_path.unlink()
            return
        if self._lock_path.exists():
            with contextlib.suppress(OSError):
                self._lock_path.unlink()
        if self._mutex is not None:
            with contextlib.suppress(Exception):  # pragma: no cover - 平台相关
                if _IS_WINDOWS:
                    _kernel32.CloseHandle(self._mutex)  # type: ignore[union-attr]
            self._mutex = None
        self._held = False

    def __enter__(self) -> SingleInstanceLock:
        if not self.acquire():
            raise RuntimeError("另一个脚本实例正在运行")
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()

    # ------------------------------------------------------------------
    def _mutex_name(self) -> str:
        digest = hashlib.sha256(str(self.data_dir.resolve()).encode("utf-8")).hexdigest()[:16]
        return f"Local\\{self._name}_{digest}"

    def _acquire_mutex(self) -> bool:
        if not _IS_WINDOWS:  # pragma: no cover - 离线/非 Windows 测试分支
            return self._acquire_file_lock()
        with contextlib.suppress(Exception):
            ctypes.set_last_error(0)
        handle = _kernel32.CreateMutexW(None, False, self._mutex_name())  # type: ignore[union-attr]
        if not handle:
            logger.error("创建单实例互斥量失败，按未获取处理")
            return False
        already = ctypes.get_last_error() == _ERROR_ALREADY_EXISTS  # type: ignore[attr-defined]
        if already:
            with contextlib.suppress(Exception):  # pragma: no cover
                _kernel32.CloseHandle(handle)  # type: ignore[union-attr]
            return False
        self._mutex = handle
        return True

    def _acquire_file_lock(self) -> bool:  # pragma: no cover - 非 Windows 兜底
        try:
            fd = os.open(self._lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False
        try:
            os.write(fd, b"{}")
        finally:
            os.close(fd)
        return True

    def _write_lock_file(self) -> None:
        payload = {
            "pid": os.getpid(),
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "cwd": str(Path.cwd()),
            "executable": sys.executable,
        }
        try:
            self._lock_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        except OSError as exc:  # 锁文件只是诊断信息，写失败不影响运行
            logger.debug("写单实例锁文件失败: %s", exc)

    def _read_lock_file(self) -> dict[str, Any] | None:
        try:
            raw = self._lock_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None


__all__ = ["SingleInstanceLock"]
