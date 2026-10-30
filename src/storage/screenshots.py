"""截图目录管理（详细设计 11.6 + 实现补充 P1-12）。

两类截图严格分开：
- ``screenshots/live/<session>/<battle>/``：战斗内临时截图，战斗结束/回港后删除；
- ``screenshots/diagnostics/``：诊断/复核截图（死亡判定、异常恢复、导航放弃、校验失败等），
  按配额（文件数 + 总容量）保留，超出后删除最旧，不参与启动清理。

启动清理只处理 ``live/`` 下不属于当前会话的遗留目录（进程崩溃可能留下），
绝不触碰 diagnostics 与标定模板。

磁盘策略（详细设计 13.4）：剩余空间低于 ``disk_warn_gb`` 时停止保存截图（只写文本日志），
低于 ``disk_pause_gb`` 时由调用方进入 PAUSE（本模块只报告状态）。
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from ..common.clock import Clock, SystemClock
from ..config.models import GlobalConfig, LoggingConfig

# 临时战斗截图子目录（与 diagnostics 区分）
LIVE_DIRNAME = "live"
# 会话目录名前缀
SESSION_DIR_PREFIX = "session_"
# 战斗目录名前缀与编号宽度
BATTLE_DIR_PREFIX = "battle_"
BATTLE_DIR_WIDTH = 4
# 默认配置（与 models.LoggingConfig 对齐）
_DEFAULT_SCREENSHOT_DIR = "screenshots"
_DEFAULT_DIAGNOSTICS_DIR = "screenshots/diagnostics"
_DEFAULT_MAX_FILES = 200
_DEFAULT_MAX_MB = 100.0
_DEFAULT_DISK_WARN_GB = 2.0
_DEFAULT_DISK_PAUSE_GB = 0.5
_BYTES_PER_MB = 1024 * 1024
_BYTES_PER_GB = 1024 * 1024 * 1024


class DiskStatus(StrEnum):
    """磁盘剩余空间状态（详细设计 13.4）。"""

    OK = "ok"
    WARN = "warn"  # 只写文本日志，不存截图
    PAUSE = "pause"  # 建议进入 PAUSED


class ScreenshotManager:
    """管理 live 临时截图、diagnostics 配额留存与启动清理。"""

    def __init__(
        self,
        project_root: Path | str,
        *,
        screenshot_dir: Path | str | None = None,
        diagnostics_dir: Path | str | None = None,
        delete_after_battle: bool = True,
        screenshot_on_error: bool = True,
        diagnostics_max_files: int = _DEFAULT_MAX_FILES,
        diagnostics_max_mb: float = _DEFAULT_MAX_MB,
        disk_warn_gb: float = _DEFAULT_DISK_WARN_GB,
        disk_pause_gb: float = _DEFAULT_DISK_PAUSE_GB,
        clock: Clock | None = None,
        disk_free_gb: Callable[[Path], float] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._root = Path(project_root).resolve()
        self._screenshot_root = (
            Path(screenshot_dir) if screenshot_dir is not None else self._root / _DEFAULT_SCREENSHOT_DIR
        )
        self._live_root = self._screenshot_root / LIVE_DIRNAME
        self._diagnostics_dir = (
            Path(diagnostics_dir)
            if diagnostics_dir is not None
            else self._root / _DEFAULT_DIAGNOSTICS_DIR
        )
        self._delete_after_battle = bool(delete_after_battle)
        self._screenshot_on_error = bool(screenshot_on_error)
        self._diagnostics_max_files = max(0, int(diagnostics_max_files))
        self._diagnostics_max_mb = max(0.0, float(diagnostics_max_mb))
        self._disk_warn_gb = max(0.0, float(disk_warn_gb))
        self._disk_pause_gb = max(0.0, float(disk_pause_gb))
        self._clock = clock or SystemClock()
        self._disk_free_gb = disk_free_gb or _default_disk_free_gb
        self._logger = logger or logging.getLogger("wows_script.storage.screenshots")
        self._session_dir: Path | None = None
        self._battle_dirs: dict[int, Path] = {}

    @classmethod
    def from_config(
        cls,
        project_root: Path | str,
        config: GlobalConfig,
        *,
        clock: Clock | None = None,
        logger: logging.Logger | None = None,
        disk_free_gb: Callable[[Path], float] | None = None,
    ) -> ScreenshotManager:
        """按全局配置构造（main 使用）。"""
        logging_config: LoggingConfig = config.logging
        return cls(
            project_root,
            screenshot_dir=config.paths.screenshot_dir,
            diagnostics_dir=_resolve(config.logging.diagnostics_dir, project_root),
            delete_after_battle=logging_config.delete_screenshots_after_battle,
            screenshot_on_error=logging_config.screenshot_on_error,
            diagnostics_max_files=logging_config.diagnostics_max_files,
            diagnostics_max_mb=logging_config.diagnostics_max_mb,
            disk_warn_gb=logging_config.disk_warn_gb,
            disk_pause_gb=logging_config.disk_pause_gb,
            clock=clock,
            disk_free_gb=disk_free_gb,
            logger=logger,
        )

    # ------------------------------------------------------------------
    # 只读属性
    # ------------------------------------------------------------------
    @property
    def root(self) -> Path:
        return self._root

    @property
    def screenshot_root(self) -> Path:
        return self._screenshot_root

    @property
    def live_root(self) -> Path:
        return self._live_root

    @property
    def diagnostics_dir(self) -> Path:
        return self._diagnostics_dir

    @property
    def session_dir(self) -> Path | None:
        return self._session_dir

    @property
    def screenshot_on_error(self) -> bool:
        return self._screenshot_on_error

    def battle_dir(self, battle_id: int) -> Path | None:
        return self._battle_dirs.get(int(battle_id))

    # ------------------------------------------------------------------
    # 会话与战斗目录
    # ------------------------------------------------------------------
    def begin_session(
        self,
        session_id: int | None = None,
        *,
        started_at: datetime | None = None,
    ) -> Path:
        """创建本会话的 live 目录，并清理上一次会话遗留的 live 目录。"""
        removed = self.cleanup_stale_live()
        if removed:
            self._logger.info("启动清理了 %d 个遗留的临时截图目录", len(removed))
        moment = started_at or self._clock.wall_now()
        name = f"{SESSION_DIR_PREFIX}{moment:%Y%m%d_%H%M%S}"
        if session_id is not None:
            name = f"{name}_s{session_id}"
        session_dir = self._live_root / name
        session_dir.mkdir(parents=True, exist_ok=True)
        self._diagnostics_dir.mkdir(parents=True, exist_ok=True)
        self._session_dir = session_dir
        self._battle_dirs.clear()
        self._logger.info("临时截图目录: %s", session_dir)
        return session_dir

    def cleanup_stale_live(self) -> list[Path]:
        """删除 live/ 下不属于当前会话的目录（进程崩溃遗留），返回删除列表。"""
        if not self._live_root.is_dir():
            return []
        removed: list[Path] = []
        for entry in sorted(self._live_root.iterdir()):
            if not entry.is_dir():
                continue
            if self._session_dir is not None and entry.resolve() == self._session_dir.resolve():
                continue
            if _remove_tree(entry):
                removed.append(entry)
        return removed

    def begin_battle(self, battle_id: int) -> Path:
        """创建本场战斗的临时截图目录。"""
        if self._session_dir is None:
            self.begin_session()
        assert self._session_dir is not None
        battle_dir = self._session_dir / f"{BATTLE_DIR_PREFIX}{int(battle_id):0{BATTLE_DIR_WIDTH}d}"
        battle_dir.mkdir(parents=True, exist_ok=True)
        self._battle_dirs[int(battle_id)] = battle_dir
        return battle_dir

    def end_battle(self, battle_id: int) -> bool:
        """战斗结束：按配置删除该场次 live 截图目录，返回是否删除。"""
        battle_id = int(battle_id)
        battle_dir = self._battle_dirs.pop(battle_id, None)
        if battle_dir is None and self._session_dir is not None:
            candidate = self._session_dir / f"{BATTLE_DIR_PREFIX}{battle_id:0{BATTLE_DIR_WIDTH}d}"
            battle_dir = candidate if candidate.is_dir() else None
        if battle_dir is None:
            return False
        if not self._delete_after_battle:
            self._logger.info("保留战斗 %s 的临时截图（配置 delete_screenshots_after_battle=false）", battle_id)
            return False
        removed = _remove_tree(battle_dir)
        if removed:
            self._logger.info("已删除战斗 %s 的临时截图目录", battle_id)
        return removed

    def cleanup_session(self) -> bool:
        """会话结束：删除整个 live 会话目录（未按场次删除的残留）。"""
        if self._session_dir is None:
            return False
        removed = _remove_tree(self._session_dir)
        self._session_dir = None
        self._battle_dirs.clear()
        return removed

    # ------------------------------------------------------------------
    # 保存截图
    # ------------------------------------------------------------------
    def disk_status(self) -> DiskStatus:
        """按磁盘剩余空间返回状态；低于 pause 阈值时调用方应进入 PAUSED。"""
        free_gb = float(self._disk_free_gb(self._screenshot_root))
        if free_gb < self._disk_pause_gb:
            return DiskStatus.PAUSE
        if free_gb < self._disk_warn_gb:
            return DiskStatus.WARN
        return DiskStatus.OK

    def can_capture(self) -> bool:
        """是否允许保存截图（磁盘空间充足）。"""
        status = self.disk_status()
        if status is not DiskStatus.OK:
            self._logger.warning("磁盘剩余空间不足（%s），本次不保存截图", status.value)
            return False
        return True

    def capture_battle(self, battle_id: int, kind: str, image: Any) -> Path | None:
        """保存一张战斗内临时截图，返回文件路径；空间不足或失败时返回 None。"""
        if not self.can_capture():
            return None
        battle_dir = self.battle_dir(battle_id) or self.begin_battle(battle_id)
        path = battle_dir / self._filename(kind)
        return path if self._write_png(path, image) else None

    def save_error(self, battle_id: int | None, kind: str, image: Any) -> Path | None:
        """保存异常/关键节点截图：配置 ``screenshot_on_error=false`` 时跳过。"""
        if not self._screenshot_on_error:
            return None
        if battle_id is None:
            return self.save_diagnostic(kind, image)
        return self.capture_battle(battle_id, kind, image)

    def save_diagnostic(self, kind: str, image: Any) -> Path | None:
        """保存一张诊断截图到 diagnostics/，并按配额清理最旧文件。"""
        if not self.can_capture():
            return None
        self._diagnostics_dir.mkdir(parents=True, exist_ok=True)
        path = self._diagnostics_dir / self._filename(kind)
        if not self._write_png(path, image):
            return None
        removed = self.prune_diagnostics()
        if removed:
            self._logger.info("诊断截图超出配额，已删除 %d 个最旧文件", len(removed))
        return path

    def prune_diagnostics(self) -> list[Path]:
        """按 ``diagnostics_max_files`` / ``diagnostics_max_mb`` 配额删除最旧的诊断截图。"""
        if not self._diagnostics_dir.is_dir():
            return []
        files = [entry for entry in self._diagnostics_dir.iterdir() if entry.is_file()]
        if not files:
            return []
        files.sort(key=lambda entry: (entry.stat().st_mtime, entry.name))
        total_bytes = sum(entry.stat().st_size for entry in files)
        limit_bytes = int(self._diagnostics_max_mb * _BYTES_PER_MB)
        removed: list[Path] = []
        while files and (len(files) > self._diagnostics_max_files or total_bytes > limit_bytes):
            oldest = files.pop(0)
            try:
                size = oldest.stat().st_size
                oldest.unlink()
            except OSError:
                continue
            total_bytes -= size
            removed.append(oldest)
        return removed

    def cleanup_all_live(self) -> list[Path]:
        """删除 live/ 下全部目录（测试/维护用；正常流程用 cleanup_stale_live）。"""
        if not self._live_root.is_dir():
            return []
        removed: list[Path] = []
        for entry in sorted(self._live_root.iterdir()):
            if entry.is_dir() and _remove_tree(entry):
                removed.append(entry)
        self._session_dir = None
        self._battle_dirs.clear()
        return removed

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------
    def _filename(self, kind: str) -> str:
        stamp = self._clock.wall_now().strftime("%Y%m%d_%H%M%S")
        safe_kind = "".join(char if char.isalnum() or char in "-_" else "_" for char in str(kind)) or "shot"
        return f"{stamp}_{safe_kind}.png"

    def _write_png(self, path: Path, image: Any) -> bool:
        if image is None:
            self._logger.warning("截图为空，跳过保存: %s", path)
            return False
        try:
            import cv2

            success, buffer = cv2.imencode(".png", image)
            if not success:
                self._logger.warning("截图编码失败: %s", path)
                return False
            buffer.tofile(str(path))
        except (OSError, ValueError) as exc:
            self._logger.warning("截图写入失败 %s: %s", path, exc)
            return False
        except Exception as exc:  # pragma: no cover - OpenCV 环境异常
            self._logger.warning("截图写入异常 %s: %s", path, exc)
            return False
        return True


# ---------------------------------------------------------------------------
# 模块级工具
# ---------------------------------------------------------------------------


def _resolve(value: str | Path, base: Path | str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else Path(base).resolve() / path


def _default_disk_free_gb(path: Path) -> float:
    """返回 path 所在磁盘的剩余空间（GB）；路径不存在时查最近的已存在祖先。"""
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free / _BYTES_PER_GB
    except OSError:
        return 0.0


def _remove_tree(path: Path) -> bool:
    """递归删除目录，失败返回 False（调用方记日志，不抛出）。"""
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return True
