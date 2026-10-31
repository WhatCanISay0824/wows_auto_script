"""环境检查工具（详细设计 12.5 / 第 16 章 V27、V28）。

分两级检查：

- **offline**：不要求游戏，验证离线链路是否可用
  （配置校验、底图 ORB 自检、合成小地图几何往返、GDI/DPI 能力探测、
  数据库可写、tkinter 可用、按键表覆盖）；
- **installed**：要求游戏在线并人工配合
  （窗口查找、客户区尺寸与截图尺寸一致、截屏非黑屏、小地图区域可截、
  船只卡片匹配、绝对点击/相对移动/按键/紧急停止热键人工确认）。

默认模式是 auto：先尝试 installed，找不到游戏窗口时降级为 offline 并明确提示。
无游戏时 offline 会为 installed 专属检查各记一条 SKIP，因此退出码为 3。

退出码：``0`` 全部 PASS；``3`` 存在 SKIP 且没有 FAIL；``2`` 存在 FAIL。
检查结果写入 ``logs/env_check.txt``（每项状态/耗时/备注），同时打印到控制台。
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import sqlite3
import sys
import tempfile
import time
import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from ..common.clock import iso_timestamp
from ..common.coords import ImageOrientation, expected_minimap_scale
from ..common.errors import CaptureUnavailable
from ..common.types import Action, Rect
from ..config.loader import LoadedProject, load_project
from ..config.models import AppConfig, MapPack, PerceptionConfig
from ..config.validate import ValidationReport, validate_project
from .calibrate_common import write_image

EXIT_OK = 0
EXIT_FAIL = 2
EXIT_SKIP = 3

#: “待实机标定”类校验错误码：这些缺失属于 M0 阶段预期状态，环境检查记为 SKIP 而不是 FAIL
PENDING_CALIBRATION_CODES: frozenset[str] = frozenset(
    {
        "file_missing",  # 模板/底图缺失（待标定）
        "file_unreadable",
        "image_unreadable",
        "image_too_small",
        "image_uniform",
        "empty",  # 尚无地图包
    }
)

#: 合成自检场景参数（与 docs/plans 的感知测试同构，但独立实现在工具内，不依赖 tests）
SELF_CHECK_BG_SIZE = 1400
SELF_CHECK_IMAGE_BOUNDS = (100.0, 80.0, 1300.0, 1280.0)
SELF_CHECK_WORLD_BOUNDS = (-6000.0, -6000.0, 6000.0, 6000.0)
SELF_CHECK_MINIMAP_SIZE = 336
SELF_CHECK_SCALE = SELF_CHECK_MINIMAP_SIZE / (
    SELF_CHECK_IMAGE_BOUNDS[2] - SELF_CHECK_IMAGE_BOUNDS[0]
)
SELF_CHECK_ROUNDTRIP_TOLERANCE_PX = 1e-6

#: installed 专属检查名称（offline 模式统一记为 SKIP）
INSTALLED_CHECK_NAMES: tuple[str, ...] = (
    "game_window",
    "client_size",
    "capture_not_black",
    "minimap_region",
    "ship_card",
    "manual_click_abs",
    "manual_move_rel",
    "manual_key",
    "manual_emergency_stop",
)


class CheckStatus(StrEnum):
    """单项检查状态。"""

    PASS = "PASS"
    SKIP = "SKIP"
    FAIL = "FAIL"


@dataclass(frozen=True, slots=True)
class CheckResult:
    """一项检查的结果：状态 + 耗时 + 备注 + 明细行。"""

    name: str
    status: CheckStatus
    duration_ms: float = 0.0
    message: str = ""
    details: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EnvironmentReport:
    """一次环境检查的完整报告。"""

    mode: str
    results: tuple[CheckResult, ...] = ()
    report_path: Path | None = None
    notes: tuple[str, ...] = ()
    created_at: str = ""

    @property
    def passed(self) -> list[CheckResult]:
        return [item for item in self.results if item.status is CheckStatus.PASS]

    @property
    def skipped(self) -> list[CheckResult]:
        return [item for item in self.results if item.status is CheckStatus.SKIP]

    @property
    def failed(self) -> list[CheckResult]:
        return [item for item in self.results if item.status is CheckStatus.FAIL]

    @property
    def exit_code(self) -> int:
        if self.failed:
            return EXIT_FAIL
        if self.skipped:
            return EXIT_SKIP
        return EXIT_OK

    def format_lines(self, root: Path) -> list[str]:
        counts = f"PASS={len(self.passed)} SKIP={len(self.skipped)} FAIL={len(self.failed)}"
        lines = [
            "wows_script 环境检查报告",
            f"生成时间: {self.created_at}",
            f"项目根目录: {root}",
            f"检查模式: {self.mode}",
            "",
        ]
        for item in self.results:
            lines.append(f"[{item.status.value}] {item.name} ({item.duration_ms:.1f} ms) - {item.message}")
            for detail in item.details:
                lines.append(f"        {detail}")
        lines.append("")
        lines.extend(self.notes)
        lines.append(f"汇总: {counts}")
        lines.append(f"退出码: {self.exit_code}（0=全过 3=有 SKIP 2=有 FAIL）")
        return lines


# ---------------------------------------------------------------------------
# 检查执行辅助
# ---------------------------------------------------------------------------


def _run(name: str, check: Callable[[], CheckResult | tuple[CheckStatus, str, Sequence[str]]]) -> CheckResult:
    """执行单项检查：计时、捕获异常转 FAIL，保证工具永不因单项检查崩溃。"""
    started = time.perf_counter()
    try:
        outcome = check()
    except Exception as exc:  # noqa: BLE001 - 环境检查需要把异常变成可读结果
        elapsed = (time.perf_counter() - started) * 1000.0
        return CheckResult(
            name=name,
            status=CheckStatus.FAIL,
            duration_ms=elapsed,
            message=f"检查过程异常: {type(exc).__name__}: {exc}",
            details=tuple(traceback.format_exc(limit=2).splitlines()[-3:]),
        )
    elapsed = (time.perf_counter() - started) * 1000.0
    if isinstance(outcome, CheckResult):  # pragma: no cover - 当前所有检查都返回元组
        return CheckResult(
            name=name,
            status=outcome.status,
            duration_ms=elapsed,
            message=outcome.message,
            details=outcome.details,
        )
    status, message, details = outcome
    return CheckResult(
        name=name,
        status=status,
        duration_ms=elapsed,
        message=message,
        details=tuple(details),
    )


def _skip_all(names: Sequence[str], message: str) -> list[CheckResult]:
    return [CheckResult(name=name, status=CheckStatus.SKIP, message=message) for name in names]


# ---------------------------------------------------------------------------
# offline 检查
# ---------------------------------------------------------------------------


def check_config(root: Path, profile_id: str, ship_id: str | None) -> CheckResult:
    """配置校验：把“待实机标定”类错误归类为 SKIP，真正的结构错误才是 FAIL。"""
    report: ValidationReport = validate_project(root, profile_id, ship_id=ship_id)
    if report.ok:
        return CheckStatus.PASS, f"无错误，{len(report.warnings)} 个警告", [
            issue.format() for issue in report.warnings[:5]
        ]
    errors = report.errors
    pending = [issue for issue in errors if issue.code in PENDING_CALIBRATION_CODES]
    blocking = [issue for issue in errors if issue.code not in PENDING_CALIBRATION_CODES]
    if blocking:
        return (
            CheckStatus.FAIL,
            f"{len(blocking)} 个需要修复的配置错误（另有 {len(pending)} 个待标定项）",
            [issue.format() for issue in blocking[:8]],
        )
    return (
        CheckStatus.SKIP,
        f"{len(pending)} 个待实机标定项（模板/底图缺失属 M0 预期状态），{len(report.warnings)} 个警告",
        [issue.format() for issue in pending[:5]],
    )


def check_orb_maps(config: AppConfig) -> CheckResult:
    """底图 ORB 特征自检（对应待确认项 V27：外部底图能否被小地图特征匹配）。"""
    from ..perception.map_identify import MapIdentifier

    maps = dict(config.maps)
    if not maps:
        return CheckStatus.SKIP, "没有任何地图包（M0 需先完成一张地图标定）", ()
    profile = config.profile
    reference_width = 320.0
    if profile.minimap is not None:
        ref_w, ref_h = profile.reference_resolution
        if ref_w > 0 and ref_h > 0:
            reference_width = float(profile.minimap.rect.to_pixels(ref_w, ref_h).w)
            reference_width = max(32.0, reference_width - 2.0 * profile.minimap.border_px)
    identifier = MapIdentifier(maps, config.global_config.perception)
    identifier.prepare(reference_minimap_width=reference_width)
    prepared = set(identifier.prepared_map_ids)
    failed = sorted(map_id for map_id in maps if map_id not in prepared)
    details = [f"ORB 特征就绪: {', '.join(sorted(prepared)) or '无'}"]
    if failed:
        details.extend(identifier.issues[:8])
        return (
            CheckStatus.FAIL,
            f"{len(failed)}/{len(maps)} 张地图的底图 ORB 特征构建失败: {', '.join(failed)}",
            details,
        )
    return CheckStatus.PASS, f"{len(prepared)} 张地图的底图 ORB 特征构建成功", details


def _synthetic_background(
    width: int = SELF_CHECK_BG_SIZE, height: int = SELF_CHECK_BG_SIZE, seed: int = 7
) -> np.ndarray:
    """生成带丰富角点/纹理的合成底图（仅用于离线自检，不代替实机标定）。"""
    rng = np.random.default_rng(seed)
    image = np.full((height, width, 3), 35, dtype=np.uint8)
    for _ in range(80):
        points = rng.integers(0, [width, height], size=(3, 2)).astype(np.int32)
        color = tuple(int(value) for value in rng.integers(50, 255, 3))
        cv2.fillPoly(image, [points], color)
    for _ in range(50):
        center = tuple(int(value) for value in rng.integers(0, [width, height], 2))
        radius = int(rng.integers(4, 24))
        color = tuple(int(value) for value in rng.integers(0, 255, 3))
        cv2.circle(image, center, radius, color, thickness=-1)
    for _ in range(30):
        start = tuple(int(value) for value in rng.integers(0, [width, height], 2))
        end = tuple(int(value) for value in rng.integers(0, [width, height], 2))
        color = tuple(int(value) for value in rng.integers(80, 255, 3))
        cv2.line(image, start, end, color, thickness=int(rng.integers(1, 5)))
    return image


def _roundtrip_error(
    transform_matrix: Any,
    points: Sequence[tuple[float, float]],
) -> float:
    """用仿射矩阵（2x3 展开或 3x3）做一次正反变换，返回最大往返误差（像素）。"""
    from ..common.coords import affine_to_matrix, apply_affine, invert_affine

    forward = np.asarray(transform_matrix, dtype=np.float64)
    if forward.shape == (6,):
        forward = affine_to_matrix(tuple(float(value) for value in forward))
    inverse = invert_affine(forward)
    worst = 0.0
    for x, y in points:
        mapped = apply_affine(forward, x, y)
        back = apply_affine(inverse, mapped[0], mapped[1])
        worst = max(worst, float(np.hypot(back[0] - x, back[1] - y)))
    return worst


def check_geometry_roundtrip() -> CheckResult:
    """合成小地图几何往返 + ORB 识别自检（详细设计 14.3 往返误差 < 1 像素）。"""
    from ..common.coords import BattleCalibration, CalibrationMode
    from ..perception.map_identify import MapIdentifier

    with tempfile.TemporaryDirectory(prefix="wows_selfcheck_") as workspace:
        map_dir = Path(workspace) / "selfcheck"
        map_dir.mkdir(parents=True, exist_ok=True)
        background = _synthetic_background()
        background_path = write_image(map_dir / "background.png", background)
        image_bounds = SELF_CHECK_IMAGE_BOUNDS
        pack = MapPack(
            map_id="selfcheck",
            name="环境自检合成地图",
            size_meters=(12000.0, 12000.0),
            world_bounds=SELF_CHECK_WORLD_BOUNDS,
            image_bounds=image_bounds,
            image_orientation=ImageOrientation.NORMAL,
            pack_dir=map_dir,
            background_path=background_path,
            background_size=(background.shape[1], background.shape[0]),
        )
        transform = pack.transform()
        x_min, y_min, x_max, y_max = SELF_CHECK_WORLD_BOUNDS
        corners = ((x_min, y_min), (x_min, y_max), (x_max, y_min), (x_max, y_max))
        world_image_error = _roundtrip_error(
            transform.world_to_image_affine,
            [transform.world_to_image(x, y) for x, y in corners],
        )

        minimap_rect = Rect(100, 50, SELF_CHECK_MINIMAP_SIZE, SELF_CHECK_MINIMAP_SIZE)
        calibration = BattleCalibration(
            world_image=transform,
            minimap_rect=minimap_rect,
            mode=CalibrationMode.FULL_MAP,
            window_rect=Rect(0, 0, 2560, 1600),
        )
        minimap_points = [calibration.world_to_minimap(x, y) for x, y in corners]
        world_minimap_error = _roundtrip_error(calibration.world_to_minimap_matrix(), minimap_points)

        crop = background[
            int(image_bounds[1]) : int(image_bounds[3]), int(image_bounds[0]) : int(image_bounds[2])
        ]
        scale = SELF_CHECK_MINIMAP_SIZE / crop.shape[1]
        matrix = np.array([[scale, 0.0, 0.0], [0.0, scale, 0.0]], dtype=np.float64)
        synthetic_minimap = cv2.warpAffine(
            crop, matrix, (SELF_CHECK_MINIMAP_SIZE, SELF_CHECK_MINIMAP_SIZE), flags=cv2.INTER_AREA
        )
        expected_scale = expected_minimap_scale(minimap_rect, transform)
        identifier = MapIdentifier({"selfcheck": pack}, PerceptionConfig())
        match = identifier.identify(synthetic_minimap, expected_scale=expected_scale, timestamp=0.0)
        details = [
            f"合成底图: {background.shape[1]}x{background.shape[0]}，可游玩区 1200x1200，"
            f"合成小地图 {SELF_CHECK_MINIMAP_SIZE}x{SELF_CHECK_MINIMAP_SIZE}（期望缩放 {expected_scale:.4f}）",
            f"世界↔底图往返最大误差: {world_image_error:.9f} px",
            f"世界↔小地图往返最大误差: {world_minimap_error:.9f} px",
        ]
        if world_image_error > SELF_CHECK_ROUNDTRIP_TOLERANCE_PX:
            return CheckStatus.FAIL, f"世界↔底图往返误差过大: {world_image_error:.6f} px", details
        if world_minimap_error > SELF_CHECK_ROUNDTRIP_TOLERANCE_PX:
            return CheckStatus.FAIL, f"世界↔小地图往返误差过大: {world_minimap_error:.6f} px", details
        if match is None:
            details.extend(identifier.issues[:8])
            return CheckStatus.FAIL, "合成小地图 ORB 识别失败（地图识别链路不可用）", details
        scale_error = abs(match.scale - expected_scale) / expected_scale
        details.extend(
            [
                f"合成小地图识别: map_id={match.map_id} 内点={match.inliers} "
                f"重投影误差={match.reprojection_error:.2f}px 识别缩放={match.scale:.4f}",
                f"识别缩放相对误差: {scale_error:.2%}",
            ]
        )
        if scale_error > 0.1:
            return CheckStatus.FAIL, f"合成小地图缩放误差过大: {scale_error:.2%}", details
        return (
            CheckStatus.PASS,
            f"几何往返误差 {max(world_image_error, world_minimap_error):.9f} px；"
            f"合成小地图识别成功（内点 {match.inliers}）",
            details,
        )


def check_gdi_dpi() -> CheckResult:
    """GDI / DPI 能力探测：只探测接口可用性，不要求游戏窗口。"""
    if sys.platform != "win32":
        return CheckStatus.SKIP, "非 Windows 平台，GDI 截屏不可用（项目只支持 Windows）", ()
    from ..common.window import is_dpi_aware, set_dpi_awareness, virtual_screen_rect
    from ..perception.capture import GdiFrameSource

    dpi_mode = set_dpi_awareness()
    aware = is_dpi_aware()
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetDC.argtypes = [ctypes.c_void_p]
    user32.GetDC.restype = ctypes.c_void_p
    user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.ReleaseDC.restype = ctypes.c_int
    desktop_dc = int(user32.GetDC(None) or 0)
    if not desktop_dc:
        return CheckStatus.FAIL, "GetDC(桌面) 失败，GDI 接口不可用", ()
    user32.ReleaseDC(None, ctypes.c_void_p(desktop_dc))
    screen = virtual_screen_rect()
    source = GdiFrameSource(dpi_aware=False)
    try:
        source_dpi = source.dpi_awareness
    finally:
        source.close()
    return (
        CheckStatus.PASS,
        f"GDI 可用；DPI 声明={dpi_mode}（感知={aware}，截屏源={source_dpi}）；"
        f"虚拟桌面 {screen.w}x{screen.h}",
        [f"虚拟桌面起点: ({screen.x}, {screen.y})"],
    )

def check_database_writable(config: AppConfig) -> CheckResult:
    """数据库可写性：临时库读写 + 正式 data 目录可写探测（不写正式库）。"""
    with tempfile.TemporaryDirectory(prefix="wows_dbcheck_") as workspace:
        db_path = Path(workspace) / "probe.db"
        try:
            connection = sqlite3.connect(db_path)
            try:
                connection.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY, note TEXT)")
                connection.execute("INSERT INTO probe (note) VALUES ('ok')")
                connection.commit()
                value = connection.execute("SELECT note FROM probe").fetchone()[0]
            finally:
                connection.close()
        except sqlite3.Error as exc:
            return CheckStatus.FAIL, f"SQLite 临时库读写失败: {exc}", ()
        if value != "ok":
            return CheckStatus.FAIL, "SQLite 写入后读取结果不一致", ()
    data_dir = Path(config.global_config.paths.data_dir)
    probe_path = data_dir / ".env_check_probe"
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        probe_path.write_text("ok", encoding="utf-8")
        probe_path.unlink(missing_ok=True)
    except OSError as exc:
        return (
            CheckStatus.FAIL,
            f"数据目录不可写: {data_dir}（{exc}）",
            ["运行前需要保证 data/ 目录可写，否则数据库会降级为文本日志"],
        )
    return CheckStatus.PASS, f"SQLite 读写正常；数据目录可写: {data_dir}", ()


def check_tkinter() -> CheckResult:
    """tkinter 可用性：能导入模块并创建/销毁一次隐藏窗口（不进入 mainloop）。"""
    try:
        tkinter = __import__("tkinter")
    except Exception as exc:  # noqa: BLE001 - 不同发行版缺失形式不同
        return CheckStatus.FAIL, f"tkinter 不可用，标定 GUI 无法启动: {exc}", ()
    try:
        root = tkinter.Tk()
        try:
            root.withdraw()
            root.update_idletasks()
        finally:
            root.destroy()
    except Exception as exc:  # noqa: BLE001 - 无图形会话
        return (
            CheckStatus.SKIP,
            f"tkinter 已安装但无法创建窗口（可能无桌面会话）: {exc}",
            ["标定 GUI 需要在有桌面会话的环境运行"],
        )
    return CheckStatus.PASS, f"tkinter 可用（Tk {tkinter.TkVersion}），标定界面可以启动", ()


def check_keymap_coverage(config: AppConfig) -> CheckResult:
    """按键映射覆盖：keys.yaml 中的按键名必须都能被输入模块解析。"""
    try:
        from ..execution.winput import is_supported_key
    except Exception as exc:  # noqa: BLE001 - 输入模块仍在并行开发，缺失时降级
        return CheckStatus.SKIP, f"输入模块不可用，跳过按键表覆盖检查: {exc}", ()
    keys = set(config.keymap.actions.values())
    keys.update(slot.key for slot in config.keymap.consumables)
    if not keys:
        return CheckStatus.FAIL, "按键映射为空（keys.yaml actions 未配置）", ()
    unsupported = sorted(key for key in keys if not is_supported_key(key))
    if unsupported:
        return (
            CheckStatus.FAIL,
            f"输入模块不支持 {len(unsupported)} 个按键名: {', '.join(unsupported)}",
            [f"全部按键: {', '.join(sorted(keys))}"],
        )
    return CheckStatus.PASS, f"{len(keys)} 个按键名均被输入模块支持", ()


def _run_offline_checks(
    root: Path,
    profile_id: str,
    ship_id: str | None,
    config: AppConfig,
) -> list[CheckResult]:
    results: list[CheckResult] = []
    results.append(_run("config_validate", lambda: check_config(root, profile_id, ship_id)))
    results.append(_run("map_orb_selfcheck", lambda: check_orb_maps(config)))
    results.append(_run("geometry_roundtrip", check_geometry_roundtrip))
    results.append(_run("gdi_dpi_probe", check_gdi_dpi))
    results.append(_run("database_writable", lambda: check_database_writable(config)))
    results.append(_run("tkinter_available", check_tkinter))
    results.append(_run("keymap_coverage", lambda: check_keymap_coverage(config)))
    return results


# ---------------------------------------------------------------------------
# installed 检查
# ---------------------------------------------------------------------------


def _manual_confirmation(prompt: str) -> bool | None:
    """控制台人工确认；非交互终端返回 None（记为 SKIP）。"""
    if not sys.stdin.isatty():
        return None
    while True:
        try:
            answer = input(f"{prompt} [y/n]: ").strip().lower()
        except EOFError:
            return None
        if answer in {"y", "yes", "是"}:
            return True
        if answer in {"n", "no", "否"}:
            return False
        print("请输入 y 或 n。")


def _load_window_locator(config: AppConfig) -> Any:
    from ..common.window import WindowLocator

    window = config.profile.window
    return WindowLocator(window.process_names, window.title_patterns)


def _installed_checks(
    config: AppConfig,
    *,
    root: Path,
    no_manual: bool,
    timeout_s: float = 15.0,
) -> tuple[list[CheckResult], list[str]]:
    """执行 installed 检查；返回 (检查结果, 备注)。内部使用，失败时以 FAIL 记录。"""
    from ..perception.capture import GdiCapture, frame_invalid_reason

    notes: list[str] = []
    results: list[CheckResult] = []
    locator = _load_window_locator(config)

    window_info = None
    try:
        window_info = locator.find()
    except Exception as exc:  # noqa: BLE001 - 窗口枚举失败按未找到处理
        notes.append(f"窗口查找异常: {exc}")
    if window_info is None:
        results.append(
            CheckResult(
                name="game_window",
                status=CheckStatus.FAIL,
                message=(
                    f"找不到游戏窗口（进程名 {list(config.profile.window.process_names)}，"
                    f"标题 {list(config.profile.window.title_patterns)}）"
                ),
                details=("若游戏尚未安装，请使用 --offline；若客户端不同，请修改 profile.window",),
            )
        )
        results.extend(
            _skip_all(INSTALLED_CHECK_NAMES[1:], "game_window 失败，后续 installed 检查跳过")
        )
        return results, notes
    results.append(
        CheckResult(
            name="game_window",
            status=CheckStatus.PASS,
            message=(
                f"已找到窗口 hwnd={window_info.hwnd} 进程={window_info.process_name or '未知'} "
                f"客户区={window_info.client_rect.w}x{window_info.client_rect.h}"
            ),
            details=(f"前台={window_info.foreground} 最小化={window_info.minimized}",),
        )
    )

    reference = config.profile.reference_resolution
    rect = window_info.client_rect
    if reference[0] > 0 and reference[1] > 0 and (rect.w, rect.h) != reference:
        results.append(
            CheckResult(
                name="client_size",
                status=CheckStatus.FAIL,
                message=f"客户区尺寸 {rect.w}x{rect.h} 与配置 {reference[0]}x{reference[1]} 不一致",
                details=("检查游戏是否为无边框窗口、显示器缩放是否为 100%，或更新 profile.reference_resolution",),
            )
        )
    else:
        results.append(
            CheckResult(name="client_size", status=CheckStatus.PASS, message=f"客户区尺寸 {rect.w}x{rect.h} 与配置一致")
        )

    capture = GdiCapture(
        locator,
        require_foreground=config.profile.window.require_foreground,
        capture_method=config.global_config.perception.capture_method,
    )
    frame = None
    try:
        if config.profile.window.require_foreground and not window_info.foreground:
            locator.bring_to_foreground(window_info.hwnd)
        frame = capture.capture()
    except CaptureUnavailable as exc:
        results.append(
            CheckResult(name="capture_not_black", status=CheckStatus.FAIL, message=f"截屏失败: {exc}")
        )
        results.extend(
            _skip_all(INSTALLED_CHECK_NAMES[3:], "截屏失败，后续 installed 检查跳过")
        )
        return results, notes
    finally:
        capture.close()
    reason = frame_invalid_reason(frame.image)
    screenshot_path = root / "logs" / "env_check_frame.png"
    try:
        write_image(screenshot_path, frame.image)
        notes.append(f"测试截图已保存: {screenshot_path}")
    except (OSError, ValueError) as exc:  # pragma: no cover - 取决于文件系统
        notes.append(f"测试截图保存失败: {exc}")
    if reason is not None:
        results.append(CheckResult(name="capture_not_black", status=CheckStatus.FAIL, message=f"截屏画面不可用: {reason}"))
        results.extend(_skip_all(INSTALLED_CHECK_NAMES[3:], "截屏画面不可用，后续 installed 检查跳过"))
        return results, notes
    results.append(
        CheckResult(
            name="capture_not_black",
            status=CheckStatus.PASS,
            message=f"截屏成功 {frame.width}x{frame.height}，耗时 {capture.stats.last_ms:.1f} ms，画面非黑屏",
            details=(f"帧序号={frame.frame_id}",),
        )
    )

    minimap = config.profile.minimap
    if minimap is None:
        results.append(CheckResult(name="minimap_region", status=CheckStatus.SKIP, message="profile.minimap 未配置"))
    else:
        pixel_rect = minimap.rect.to_pixels(frame.width, frame.height)
        border = max(0, int(minimap.border_px))
        content = Rect(
            pixel_rect.x + border,
            pixel_rect.y + border,
            max(1, pixel_rect.w - 2 * border),
            max(1, pixel_rect.h - 2 * border),
        )
        try:
            minimap_image = frame.crop(content)
        except ValueError as exc:
            results.append(
                CheckResult(name="minimap_region", status=CheckStatus.FAIL, message=f"小地图区域裁剪失败: {exc}")
            )
        else:
            reason = frame_invalid_reason(minimap_image)
            minimap_path = root / "logs" / "env_check_minimap.png"
            try:
                write_image(minimap_path, minimap_image)
            except (OSError, ValueError):  # pragma: no cover
                pass
            else:
                notes.append(f"小地图测试截图已保存: {minimap_path}")
            if reason is not None:
                results.append(
                    CheckResult(name="minimap_region", status=CheckStatus.FAIL, message=f"小地图区域画面不可用: {reason}")
                )
            else:
                results.append(
                    CheckResult(
                        name="minimap_region",
                        status=CheckStatus.PASS,
                        message=f"小地图内容区可截取 {content.w}x{content.h}（归一化 {minimap.rect.to_list()}）",
                    )
                )

    ship = config.ship
    if ship.card_path is None:
        results.append(
            CheckResult(
                name="ship_card",
                status=CheckStatus.SKIP,
                message=f"船只卡片模板缺失（待标定）: {ship.ship_dir}",
            )
        )
    else:
        try:
            from ..common.types import NormalizedRect
            from ..perception.templates import TemplateMatcher

            matcher = TemplateMatcher(config.global_config.perception, project_root=root)
            matcher.load_profile(config.profile)
            region = ship.card_search_region or NormalizedRect(0.0, 0.0, 1.0, 1.0)
            matcher.register_feature("ship_card", ship.card_path, region=region)
            match = matcher.match("ship_card", frame)
        except Exception as exc:  # noqa: BLE001 - 模板/匹配问题记为 SKIP 并说明
            results.append(CheckResult(name="ship_card", status=CheckStatus.SKIP, message=f"船只卡片匹配未执行: {exc}"))
        else:
            if match.found:
                results.append(
                    CheckResult(
                        name="ship_card",
                        status=CheckStatus.PASS,
                        message=f"船只卡片在港口画面命中（得分 {match.score:.3f}）",
                    )
                )
            else:
                results.append(
                    CheckResult(
                        name="ship_card",
                        status=CheckStatus.SKIP,
                        message=f"船只卡片未命中（得分 {match.score:.3f}）；请确认当前停在港口界面",
                    )
                )

    results.extend(
        _manual_input_checks(
            config,
            window_rect=rect,
            no_manual=no_manual,
            timeout_s=timeout_s,
        )
    )
    return results, notes


def _manual_input_checks(
    config: AppConfig,
    *,
    window_rect: Rect,
    no_manual: bool,
    timeout_s: float,
) -> list[CheckResult]:
    """人工确认类检查：绝对点击 / 相对移动 / 按键 / 紧急停止热键。"""
    if no_manual or not sys.stdin.isatty():
        return _skip_all(INSTALLED_CHECK_NAMES[5:], "--no-manual 或非交互终端，人工确认检查跳过")
    results: list[CheckResult] = []
    try:
        from ..execution.winput import make_backend
    except Exception as exc:  # noqa: BLE001
        return _skip_all(INSTALLED_CHECK_NAMES[5:], f"输入模块不可用: {exc}")

    try:
        backend = make_backend(config.global_config.input)
    except Exception as exc:  # noqa: BLE001
        return _skip_all(INSTALLED_CHECK_NAMES[5:], f"无法创建输入后端: {exc}")

    center_x, center_y = window_rect.center
    try:
        print(f"\n即将在游戏窗口中心 ({center_x}, {center_y}) 进行一次绝对坐标点击，请观察游戏反应。")
        confirmed = _manual_confirmation("继续执行绝对点击测试？")
        if confirmed is None:
            results.append(CheckResult("manual_click_abs", CheckStatus.SKIP, message="非交互终端，跳过"))
        elif not confirmed:
            results.append(CheckResult("manual_click_abs", CheckStatus.SKIP, message="用户跳过"))
        else:
            backend.send([Action.click_abs(center_x, center_y, label="env_check_click")])
            observed = _manual_confirmation("游戏是否产生了对应反应（例如菜单/按钮响应）？")
            results.append(
                CheckResult(
                    "manual_click_abs",
                    CheckStatus.PASS if observed else CheckStatus.FAIL,
                    message="绝对点击可见" if observed else "绝对点击无可见反应（检查管理员权限/独占全屏/DPI）",
                )
            )
        print("\n即将发送一次鼠标相对移动（+80, 0），请观察视角或光标是否变化。")
        confirmed = _manual_confirmation("继续执行相对移动测试？")
        if confirmed is None:
            results.append(CheckResult("manual_move_rel", CheckStatus.SKIP, message="非交互终端，跳过"))
        elif not confirmed:
            results.append(CheckResult("manual_move_rel", CheckStatus.SKIP, message="用户跳过"))
        else:
            backend.send([Action.move_rel(80, 0, label="env_check_move")])
            observed = _manual_confirmation("视角/光标是否移动？")
            results.append(
                CheckResult(
                    "manual_move_rel",
                    CheckStatus.PASS if observed else CheckStatus.FAIL,
                    message="相对移动有效" if observed else "相对移动无可见反应",
                )
            )
        menu_key = config.profile.exit.menu_key or "escape"
        print(f"\n即将发送一次按键 {menu_key!r}，请观察游戏界面是否响应。")
        confirmed = _manual_confirmation("继续执行按键测试？")
        if confirmed is None:
            results.append(CheckResult("manual_key", CheckStatus.SKIP, message="非交互终端，跳过"))
        elif not confirmed:
            results.append(CheckResult("manual_key", CheckStatus.SKIP, message="用户跳过"))
        else:
            backend.send([Action.key_down(menu_key, label="env_check_key"), Action.key_up(menu_key, label="env_check_key")])
            observed = _manual_confirmation("按键是否产生预期反应？")
            results.append(
                CheckResult(
                    "manual_key",
                    CheckStatus.PASS if observed else CheckStatus.FAIL,
                    message=f"按键 {menu_key!r} 可见" if observed else f"按键 {menu_key!r} 无可见反应",
                )
            )
    finally:
        with contextlib.suppress(Exception):
            backend.release_all()
    results.append(_check_emergency_stop(config, timeout_s=timeout_s))
    return results


def _check_emergency_stop(config: AppConfig, *, timeout_s: float) -> CheckResult:
    """紧急停止热键：提示用户按下 F12 并用 GetAsyncKeyState 检测。"""
    name = "manual_emergency_stop"
    key = config.global_config.safety.emergency_stop_key or "f12"
    try:
        from ..execution.safety import Win32KeyStateSource
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name, CheckStatus.SKIP, message=f"安全模块不可用，跳过紧急停止热键检查: {exc}")
    try:
        source = Win32KeyStateSource()
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name, CheckStatus.SKIP, message=f"无法读取按键状态，跳过: {exc}")
    print(f"\n请在 {timeout_s:.0f} 秒内按下紧急停止热键 {key!r}，以便验证热键可被检测。")
    deadline = time.monotonic() + max(1.0, timeout_s)
    while time.monotonic() < deadline:
        try:
            if source.is_down(key):
                return CheckResult(name, CheckStatus.PASS, message=f"检测到紧急停止热键 {key!r} 被按下")
        except Exception as exc:  # noqa: BLE001
            return CheckResult(name, CheckStatus.SKIP, message=f"按键检测失败，跳过: {exc}")
        time.sleep(0.05)
    return CheckResult(name, CheckStatus.FAIL, message=f"{timeout_s:.0f} 秒内没有检测到紧急停止热键 {key!r}")


# ---------------------------------------------------------------------------
# 总流程
# ---------------------------------------------------------------------------


def _default_report_path(root: Path) -> Path:
    return root / "logs" / "env_check.txt"


def _write_report(report: EnvironmentReport, root: Path) -> Path | None:
    path = report.report_path or _default_report_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(report.format_lines(root)) + "\n", encoding="utf-8")
    except OSError:
        return None
    return path


def run_checks(
    root: Path | str,
    profile_id: str = "cn360",
    *,
    ship_id: str | None = None,
    offline: bool | None = None,
    no_manual: bool = False,
    report_path: Path | str | None = None,
    manual_timeout_s: float = 15.0,
) -> EnvironmentReport:
    """执行环境检查并返回报告；``offline=None`` 表示 auto（尝试 installed 后降级）。"""
    project_root = Path(root).expanduser().resolve()
    resolved_report = Path(report_path) if report_path is not None else None
    if resolved_report is not None and not resolved_report.is_absolute():
        resolved_report = project_root / resolved_report
    notes: list[str] = []
    config: AppConfig | None = None
    loaded: LoadedProject | None = None
    load_error: str | None = None
    try:
        loaded = load_project(project_root, profile_id, ship_id=ship_id)
        config = loaded.config
    except Exception as exc:  # noqa: BLE001 - 配置完全无法解析时仍要给出报告
        load_error = f"{type(exc).__name__}: {exc}"

    mode = "offline" if offline is True else "installed" if offline is False else "auto"
    results: list[CheckResult] = []
    degraded = False
    if config is None:
        results.append(
            CheckResult(
                name="project_load",
                status=CheckStatus.FAIL,
                message=f"项目配置无法加载: {load_error}",
                details=("先运行 python -m src.tools.validate --root . 查看具体问题",),
            )
        )
        results.extend(_skip_all(INSTALLED_CHECK_NAMES, "项目配置无法加载，installed 检查跳过"))
        report = EnvironmentReport(
            mode="offline（配置加载失败）",
            results=tuple(results),
            report_path=resolved_report or _default_report_path(project_root),
            notes=tuple(notes),
            created_at=iso_timestamp(datetime.now().astimezone()),
        )
        _write_report(report, project_root)
        return report

    # auto 模式：先看能否找到游戏窗口，找不到就降级 offline
    if mode == "auto":
        from ..common.window import WindowLocator

        window = config.profile.window
        try:
            game_window = WindowLocator(window.process_names, window.title_patterns).find()
        except Exception as exc:  # noqa: BLE001 - 平台/权限问题按找不到处理
            game_window = None
            notes.append(f"窗口查找异常，按未找到游戏处理: {exc}")
        if game_window is None:
            degraded = True
            offline = True
            notes.append("未找到游戏窗口：已自动降级为 offline 模式（使用 --installed 可强制要求游戏在线）")
        else:
            offline = False

    if offline is True:
        results.extend(_run_offline_checks(project_root, profile_id, ship_id, config))
        results.extend(_skip_all(INSTALLED_CHECK_NAMES, "offline 模式未执行（需要游戏窗口与人工确认）"))
        mode_label = "offline"
    else:
        results.extend(_run_offline_checks(project_root, profile_id, ship_id, config))
        try:
            installed_results, installed_notes = _installed_checks(
                config,
                root=project_root,
                no_manual=no_manual,
                timeout_s=manual_timeout_s,
            )
        except Exception as exc:  # noqa: BLE001 - installed 检查失败必须转为可读结果
            installed_results = [
                CheckResult(
                    name="installed_checks",
                    status=CheckStatus.FAIL,
                    message=f"installed 检查过程异常: {type(exc).__name__}: {exc}",
                )
            ]
            installed_notes = []
        results.extend(installed_results)
        notes.extend(installed_notes)
        mode_label = "installed" if mode == "installed" else "installed（自动检测到游戏窗口）"

    if degraded:
        mode_label = "offline（自动降级：无游戏窗口）"
    report = EnvironmentReport(
        mode=mode_label,
        results=tuple(results),
        report_path=resolved_report or _default_report_path(project_root),
        notes=tuple(notes),
        created_at=iso_timestamp(datetime.now().astimezone()),
    )
    written = _write_report(report, project_root)
    if written is None:
        notes = report.notes + ("报告文件写入失败，仅保留控制台输出",)
        report = EnvironmentReport(
            mode=report.mode,
            results=report.results,
            report_path=report.report_path,
            notes=notes,
            created_at=report.created_at,
        )
    return report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.tools.check_env",
        description="检查 wows_script 运行环境（离线链路 / 游戏窗口 / 截屏 / 输入 / 标定可用性）",
    )
    parser.add_argument("--root", default=".", help="项目根目录（包含 config.yaml），默认当前目录")
    parser.add_argument("--profile", default="cn360", help="客户端配置编号，默认 cn360")
    parser.add_argument("--ship", default=None, help="覆盖 config.yaml 的船只编号")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--offline",
        action="store_true",
        help="只运行离线检查（不要求游戏；installed 检查记为 SKIP，退出码 3）",
    )
    mode.add_argument(
        "--installed",
        action="store_true",
        help="运行需要游戏在线的检查（找不到游戏窗口时退出码 2）",
    )
    parser.add_argument(
        "--report",
        default=None,
        help="报告文件路径（默认 <root>/logs/env_check.txt）",
    )
    parser.add_argument(
        "--no-manual",
        action="store_true",
        help="跳过需要人工确认的输入检查（installed 模式）",
    )
    parser.add_argument(
        "--manual-timeout",
        type=float,
        default=15.0,
        help="人工确认类检查的等待秒数，默认 15",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """命令行入口：打印报告并返回退出码（0/3/2）。"""
    # Windows 控制台默认 GBK：报告里含 ↔ 等符号，统一改成 UTF-8 + replace 输出，
    # 避免 UnicodeEncodeError 把环境检查变成崩溃（报告本身同时写入 logs/env_check.txt）。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover - 非标准流
            pass
    args = _build_parser().parse_args(argv)
    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        print(f"[ERROR] 项目根目录不存在: {root}", file=sys.stderr)
        return EXIT_FAIL
    offline: bool | None = None
    if args.offline:
        offline = True
    elif args.installed:
        offline = False
    report = run_checks(
        root,
        args.profile,
        ship_id=args.ship,
        offline=offline,
        no_manual=bool(args.no_manual),
        report_path=args.report,
        manual_timeout_s=float(args.manual_timeout),
    )
    lines = report.format_lines(root)
    print("\n".join(lines))
    if report.report_path is not None:
        print(f"\n报告文件: {report.report_path}")
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
