"""截图目录管理单元测试（T1：src/storage/screenshots.py，详细设计 11.6 + P1-12）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2
import numpy as np
import pytest
from src.common.clock import FakeClock
from src.config.loader import load_global
from src.storage.screenshots import (
    BATTLE_DIR_PREFIX,
    LIVE_DIRNAME,
    DiskStatus,
    ScreenshotManager,
)

from tests.fixtures.configs import builder as fx

TZ_CN = timezone(timedelta(hours=8))
WALL_START = datetime(2026, 1, 1, 12, 0, 0, tzinfo=TZ_CN)


class DiskSpace:
    """可注入的磁盘剩余空间假实现。"""

    def __init__(self, free_gb: float = 10.0) -> None:
        self.free_gb = free_gb
        self.calls = 0

    def __call__(self, path: Path) -> float:
        self.calls += 1
        return self.free_gb


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(wall_start=WALL_START)


@pytest.fixture
def disk() -> DiskSpace:
    return DiskSpace()


@pytest.fixture
def manager(tmp_path: Path, clock: FakeClock, disk: DiskSpace) -> ScreenshotManager:
    return ScreenshotManager(tmp_path, clock=clock, disk_free_gb=disk, logger=_quiet_logger())


def _quiet_logger():
    import logging

    logger = logging.getLogger("tests.screenshots")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    return logger


def _read_image(path: Path) -> np.ndarray:
    data = np.fromfile(str(path), dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
    assert image is not None
    return image


# ---------------------------------------------------------------------------
# 目录结构与启动清理
# ---------------------------------------------------------------------------


def test_begin_session_creates_live_and_diagnostics(manager: ScreenshotManager, tmp_path: Path) -> None:
    session_dir = manager.begin_session()
    assert session_dir == tmp_path / "screenshots" / LIVE_DIRNAME / "session_20260101_120000"
    assert session_dir.is_dir()
    assert manager.diagnostics_dir == tmp_path / "screenshots" / "diagnostics"
    assert manager.diagnostics_dir.is_dir()
    assert manager.session_dir == session_dir


def test_startup_cleanup_only_removes_stale_live(
    manager: ScreenshotManager, tmp_path: Path, clock: FakeClock
) -> None:
    # 模拟上次进程崩溃留下的 live 目录与诊断截图
    stale = tmp_path / "screenshots" / LIVE_DIRNAME / "session_20251231_100000" / "battle_0001"
    stale.mkdir(parents=True)
    (stale / "20251231_100000_death.png").write_bytes(b"x")
    diagnostics = tmp_path / "screenshots" / "diagnostics"
    diagnostics.mkdir(parents=True)
    keep = diagnostics / "20251231_100000_error.png"
    keep.write_bytes(b"x")

    session_dir = manager.begin_session()
    assert not stale.parent.exists()
    assert keep.is_file()
    assert session_dir.is_dir()


def test_begin_session_with_session_id(manager: ScreenshotManager, tmp_path: Path) -> None:
    session_dir = manager.begin_session(session_id=7)
    assert session_dir.name == "session_20260101_120000_s7"


def test_begin_battle_creates_directory(manager: ScreenshotManager) -> None:
    manager.begin_session()
    battle_dir = manager.begin_battle(3)
    assert battle_dir.name == f"{BATTLE_DIR_PREFIX}0003"
    assert battle_dir == manager.battle_dir(3)
    assert manager.battle_dir(99) is None


def test_begin_battle_without_session_autocreates(manager: ScreenshotManager) -> None:
    battle_dir = manager.begin_battle(1)
    assert manager.session_dir is not None
    assert battle_dir.parent == manager.session_dir


# ---------------------------------------------------------------------------
# 临时截图：保存 / 删除
# ---------------------------------------------------------------------------


def test_capture_battle_writes_png(manager: ScreenshotManager, tmp_path: Path, clock: FakeClock) -> None:
    manager.begin_session()
    manager.begin_battle(1)
    image = fx.template_image(seed=7)
    path = manager.capture_battle(1, "death", image)
    assert path is not None
    assert path.name == "20260101_120000_death.png"
    assert path.parent == manager.battle_dir(1)
    saved = _read_image(path)
    assert saved.shape == image.shape

    clock.advance(5)
    second = manager.capture_battle(1, "nav_abandon", image)
    assert second is not None
    assert second.name == "20260101_120005_nav_abandon.png"


def test_capture_filename_sanitized(manager: ScreenshotManager) -> None:
    manager.begin_session()
    path = manager.capture_battle(1, "death shot/x", fx.template_image(seed=1))
    assert path is not None
    assert path.name.endswith("_death_shot_x.png")


def test_end_battle_deletes_live_directory(manager: ScreenshotManager) -> None:
    manager.begin_session()
    battle_dir = manager.begin_battle(2)
    assert manager.capture_battle(2, "death", fx.template_image(seed=2)) is not None
    assert manager.end_battle(2) is True
    assert not battle_dir.exists()
    assert manager.end_battle(2) is False


def test_end_battle_keeps_when_configured(tmp_path: Path, clock: FakeClock) -> None:
    manager = ScreenshotManager(
        tmp_path, clock=clock, disk_free_gb=DiskSpace(), delete_after_battle=False, logger=_quiet_logger()
    )
    manager.begin_session()
    battle_dir = manager.begin_battle(2)
    assert manager.capture_battle(2, "death", fx.template_image(seed=2)) is not None
    assert manager.end_battle(2) is False
    assert battle_dir.is_dir()


def test_cleanup_session(manager: ScreenshotManager) -> None:
    session_dir = manager.begin_session()
    manager.begin_battle(1)
    assert manager.cleanup_session() is True
    assert not session_dir.exists()
    assert manager.session_dir is None
    assert manager.cleanup_session() is False


# ---------------------------------------------------------------------------
# 诊断截图与配额
# ---------------------------------------------------------------------------


def test_save_diagnostic_and_error_routing(manager: ScreenshotManager) -> None:
    manager.begin_session()
    manager.begin_battle(4)
    image = fx.template_image(seed=4)
    battle_path = manager.save_error(4, "map_identify_fail", image)
    assert battle_path is not None
    assert battle_path.parent == manager.battle_dir(4)

    diagnostic_path = manager.save_error(None, "window_missing", image)
    assert diagnostic_path is not None
    assert diagnostic_path.parent == manager.diagnostics_dir
    assert _read_image(diagnostic_path).shape == image.shape


def test_screenshot_on_error_disabled(tmp_path: Path, clock: FakeClock) -> None:
    manager = ScreenshotManager(
        tmp_path, clock=clock, disk_free_gb=DiskSpace(), screenshot_on_error=False, logger=_quiet_logger()
    )
    manager.begin_session()
    assert manager.save_error(1, "death", fx.template_image(seed=1)) is None
    # 显式 capture_battle 不受 screenshot_on_error 开关影响
    assert manager.capture_battle(1, "death", fx.template_image(seed=1)) is not None


def test_prune_diagnostics_by_file_count(tmp_path: Path, clock: FakeClock) -> None:
    manager = ScreenshotManager(
        tmp_path,
        clock=clock,
        disk_free_gb=DiskSpace(),
        diagnostics_max_files=2,
        logger=_quiet_logger(),
    )
    saved: list[Path] = []
    for index in range(5):
        clock.advance(1)
        path = manager.save_diagnostic(f"shot{index}", fx.template_image(seed=index))
        assert path is not None
        saved.append(path)
    remaining = sorted(path.name for path in manager.diagnostics_dir.iterdir())
    assert remaining == sorted(path.name for path in saved[-2:])
    assert not any(path.exists() for path in saved[:3])


def test_prune_diagnostics_by_size(tmp_path: Path, clock: FakeClock) -> None:
    manager = ScreenshotManager(
        tmp_path,
        clock=clock,
        disk_free_gb=DiskSpace(),
        diagnostics_max_files=100,
        diagnostics_max_mb=0.0005,  # ≈524 字节，约 1～2 张合成图
        logger=_quiet_logger(),
    )
    for index in range(4):
        clock.advance(1)
        manager.save_diagnostic(f"shot{index}", fx.template_image(seed=index))
    files = list(manager.diagnostics_dir.iterdir())
    total = sum(path.stat().st_size for path in files)
    assert total <= int(0.0005 * 1024 * 1024)
    assert len(files) <= 1


def test_prune_diagnostics_manual(manager: ScreenshotManager, clock: FakeClock) -> None:
    manager.diagnostics_dir.mkdir(parents=True)
    for index in range(3):
        clock.advance(1)
        manager.save_diagnostic(f"m{index}", fx.template_image(seed=index))
    assert len(manager.prune_diagnostics()) == 0  # 未超配额
    assert manager.prune_diagnostics() == []


# ---------------------------------------------------------------------------
# 磁盘剩余空间策略
# ---------------------------------------------------------------------------


def test_disk_status_thresholds(tmp_path: Path, clock: FakeClock) -> None:
    disk = DiskSpace(10.0)
    manager = ScreenshotManager(
        tmp_path, clock=clock, disk_free_gb=disk, disk_warn_gb=2.0, disk_pause_gb=0.5, logger=_quiet_logger()
    )
    assert manager.disk_status() is DiskStatus.OK
    disk.free_gb = 1.0
    assert manager.disk_status() is DiskStatus.WARN
    disk.free_gb = 0.2
    assert manager.disk_status() is DiskStatus.PAUSE


def test_low_disk_stops_screenshots(tmp_path: Path, clock: FakeClock) -> None:
    disk = DiskSpace(1.0)  # 低于告警阈值 2GB
    manager = ScreenshotManager(tmp_path, clock=clock, disk_free_gb=disk, logger=_quiet_logger())
    manager.begin_session()
    assert manager.can_capture() is False
    assert manager.capture_battle(1, "death", fx.template_image(seed=1)) is None
    assert manager.save_diagnostic("death", fx.template_image(seed=1)) is None
    assert manager.begin_battle(1).is_dir()  # 目录仍创建，只是不写图


# ---------------------------------------------------------------------------
# 按配置构造
# ---------------------------------------------------------------------------


def test_from_config(tmp_path: Path, clock: FakeClock) -> None:
    project = fx.build_valid_project(tmp_path)
    global_config = load_global(tmp_path / "config.yaml", project_root=tmp_path)
    manager = ScreenshotManager.from_config(
        tmp_path, global_config, clock=clock, disk_free_gb=DiskSpace(), logger=_quiet_logger()
    )
    assert manager.screenshot_root == (tmp_path / "screenshots").resolve()
    assert manager.live_root == manager.screenshot_root / LIVE_DIRNAME
    assert manager.diagnostics_dir == (tmp_path / "screenshots" / "diagnostics").resolve()
    session_dir = manager.begin_session()
    assert manager.capture_battle(1, "error", fx.template_image(seed=1)) is not None
    assert session_dir in manager.capture_battle(1, "error2", fx.template_image(seed=2)).parents
    manager.cleanup_all_live()
    assert not session_dir.exists()
    assert project.map_dir.is_dir()  # 不触碰地图包
