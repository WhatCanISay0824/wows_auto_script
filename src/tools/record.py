"""画面录制工具（详细设计 12.4 / 评审 P2-2）。

按 ``runtime.capture_hz``（默认 2 Hz）截取游戏窗口画面并保存为 PNG 序列，同时写
``metadata.json`` 记录每帧的时间戳、帧序号、窗口矩形与人工状态标签。为控制体积
（评审 P2-2：2 Hz 全帧 PNG 一场约 4~10 GB），默认把画面降采样 2 倍；也可以用
``--roi`` 只录归一化区域、或用 ``--downscale 1`` 保留原始尺寸。

录制内容只写在本机 ``recordings/<时间戳>/`` 下，不进入 ``screenshots/`` 清理范围。
手动状态标签通过热键写入 metadata（回放时用于计算识别准确率）：

===========  ==========================
热键          状态标签
===========  ==========================
F1           PORT
F2           QUEUEING
F3           LOADING
F4           BATTLE_ACTIVE
F5           BATTLE_FINISHED
F6           DEAD_PENDING_EXIT
F7           EXIT_MENU_OPEN
F8           POST_EXIT_INTERFACE
F12          停止录制（与紧急停止热键一致）
===========  ==========================

本工具不发送任何键鼠事件；录制逻辑（``record_frames``）通过注入的截屏源、时钟与
等待器实现，单元测试用假帧源驱动，不要求游戏窗口。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from ..common.clock import Clock, Sleeper, SystemClock, SystemSleeper, iso_timestamp
from ..common.errors import CaptureUnavailable
from ..common.types import Rect, State
from ..config.loader import load_project
from ..perception.capture import Frame, GdiCapture
from .calibrate_common import write_image

logger = logging.getLogger("wows_script.tools.record")

#: metadata.json 的结构版本
RECORDING_VERSION = 1
METADATA_FILENAME = "metadata.json"
FRAMES_DIRNAME = "frames"
#: 默认输出目录前缀（相对项目根目录）
DEFAULT_OUTPUT_ROOT = "recordings"
#: 连续截屏失败达到该次数时停止录制
MAX_CONSECUTIVE_CAPTURE_FAILURES = 5
#: 状态标签热键表（详细设计 6.1 的主状态）
DEFAULT_STATE_HOTKEYS: dict[str, str] = {
    "f1": State.PORT.value,
    "f2": State.QUEUEING.value,
    "f3": State.LOADING.value,
    "f4": State.BATTLE_ACTIVE.value,
    "f5": State.BATTLE_FINISHED.value,
    "f6": State.DEAD_PENDING_EXIT.value,
    "f7": State.EXIT_MENU_OPEN.value,
    "f8": State.POST_EXIT_INTERFACE.value,
}
DEFAULT_STOP_KEY = "f12"


class FrameCapture(Protocol):
    """录制用的截屏源（真实实现是 ``GdiCapture``，测试注入假实现）。"""

    def capture(self) -> Frame:
        """抓取一帧；失败抛 ``CaptureUnavailable``。"""


class FrameLabelSource(Protocol):
    """状态标签来源：持续录制时轮询是否按下了标签热键。"""

    def poll(self) -> str | None:
        """返回本轮新触发的状态标签；没有返回 None。"""


class StopSource(Protocol):
    """录制停止信号来源（通常轮询 F12）。"""

    def should_stop(self) -> bool:
        """需要停止录制时返回 True。"""


# ---------------------------------------------------------------------------
# metadata 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FrameRecord:
    """一帧的元数据。"""

    frame_id: int
    file: str
    timestamp: float
    window_rect: tuple[int, int, int, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "frame_id": self.frame_id,
            "file": self.file,
            "ts": round(self.timestamp, 6),
            "window_rect": list(self.window_rect),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FrameRecord:
        rect = tuple(int(value) for value in data.get("window_rect", (0, 0, 0, 0)))
        return cls(
            frame_id=int(data["frame_id"]),
            file=str(data["file"]),
            timestamp=float(data.get("ts", 0.0)),
            window_rect=rect,  # type: ignore[arg-type]
        )


@dataclass(slots=True)
class RecordingMetadata:
    """一次录制的完整元数据（写入 metadata.json）。"""

    version: int = RECORDING_VERSION
    created_at: str = ""
    profile_id: str = "cn360"
    client_id: str = ""
    reference_resolution: tuple[int, int] = (0, 0)
    capture_hz: float = 2.0
    downscale: float = 1.0
    roi: list[float] | None = None
    window_rect: tuple[int, int, int, int] = (0, 0, 0, 0)
    frame_size: tuple[int, int] = (0, 0)
    frames: list[FrameRecord] = field(default_factory=list)
    labels: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    # --- 统计与查询 ---------------------------------------------------
    @property
    def frame_count(self) -> int:
        return len(self.frames)

    def state_at(self, frame_id: int) -> str | None:
        """取某帧生效的人工状态标签（最近一次不晚于该帧的标签）。"""
        current: str | None = None
        for item in self.labels:
            if int(item.get("frame_id", 0)) > frame_id:
                break
            state = item.get("state")
            if state:
                current = str(state)
        return current

    def label_at(self, frame_id: int) -> dict[str, Any] | None:
        current: dict[str, Any] | None = None
        for item in self.labels:
            if int(item.get("frame_id", 0)) > frame_id:
                break
            current = item
        return current

    def labeled_frames(self) -> list[tuple[FrameRecord, str]]:
        """返回所有“有生效标签”的帧及标签。"""
        result: list[tuple[FrameRecord, str]] = []
        for record in self.frames:
            state = self.state_at(record.frame_id)
            if state:
                result.append((record, state))
        return result

    # --- 序列化 -------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "created_at": self.created_at,
            "profile_id": self.profile_id,
            "client_id": self.client_id,
            "reference_resolution": list(self.reference_resolution),
            "capture_hz": self.capture_hz,
            "downscale": self.downscale,
            "roi": self.roi,
            "window_rect": list(self.window_rect),
            "frame_size": list(self.frame_size),
            "frames": [frame.to_dict() for frame in self.frames],
            "labels": list(self.labels),
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RecordingMetadata:
        version = int(data.get("version", RECORDING_VERSION))
        if version > RECORDING_VERSION:
            raise ValueError(f"录制版本 {version} 高于本工具支持的 {RECORDING_VERSION}")
        window_rect = tuple(int(value) for value in data.get("window_rect", (0, 0, 0, 0)))
        frame_size = tuple(int(value) for value in data.get("frame_size", (0, 0)))
        return cls(
            version=version,
            created_at=str(data.get("created_at", "")),
            profile_id=str(data.get("profile_id", "cn360")),
            client_id=str(data.get("client_id", "")),
            reference_resolution=tuple(int(value) for value in data.get("reference_resolution", (0, 0))),  # type: ignore[arg-type]
            capture_hz=float(data.get("capture_hz", 2.0)),
            downscale=float(data.get("downscale", 1.0)),
            roi=[float(value) for value in data["roi"]] if data.get("roi") else None,
            window_rect=window_rect,  # type: ignore[arg-type]
            frame_size=frame_size,  # type: ignore[arg-type]
            frames=[FrameRecord.from_dict(item) for item in data.get("frames", [])],
            labels=[dict(item) for item in data.get("labels", [])],
            notes=[str(item) for item in data.get("notes", [])],
        )


# ---------------------------------------------------------------------------
# 纯函数：帧预处理与文件读写
# ---------------------------------------------------------------------------


def resolve_downscale(value: float) -> float:
    """校验降采样倍数：1 表示原尺寸，2 表示宽高各减半。"""
    scale = float(value)
    if scale < 1.0:
        raise ValueError(f"降采样倍数必须 >= 1.0（1 表示原尺寸）: {value!r}")
    return scale


def prepare_frame_for_recording(
    image: Any,
    *,
    downscale: float = 2.0,
    roi: Rect | None = None,
) -> Any:
    """录制前的画面处理：可选裁剪 ROI + 降采样（返回新数组，不改原帧）。"""
    import cv2

    if image is None or getattr(image, "size", 0) == 0:
        raise ValueError("待录制画面为空")
    processed = image
    if roi is not None:
        height, width = processed.shape[:2]
        left = max(0, min(width, int(roi.x)))
        top = max(0, min(height, int(roi.y)))
        right = max(0, min(width, int(roi.right)))
        bottom = max(0, min(height, int(roi.bottom)))
        if right - left <= 0 or bottom - top <= 0:
            raise ValueError(f"录制 ROI 在画面内为空: {roi!r}，画面 {width}x{height}")
        processed = processed[top:bottom, left:right]
    scale = resolve_downscale(downscale)
    if scale > 1.0:
        height, width = processed.shape[:2]
        target = (max(1, int(round(width / scale))), max(1, int(round(height / scale))))
        interpolation = cv2.INTER_AREA
        processed = cv2.resize(processed, target, interpolation=interpolation)
    return processed


def write_metadata(recording_dir: Path | str, metadata: RecordingMetadata) -> Path:
    """把 metadata 写成 JSON（UTF-8、缩进 2；先写临时文件再替换，避免半截文件）。"""
    directory = Path(recording_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / METADATA_FILENAME
    temporary = directory / f".{METADATA_FILENAME}.tmp"
    text = json.dumps(metadata.to_dict(), ensure_ascii=False, indent=2)
    temporary.write_text(text + "\n", encoding="utf-8")
    temporary.replace(target)
    return target


def load_metadata(recording_dir: Path | str) -> RecordingMetadata:
    """读取录制目录的 metadata.json；缺失/损坏时抛 ``ValueError``。"""
    path = Path(recording_dir) / METADATA_FILENAME
    if not path.is_file():
        raise ValueError(f"录制目录缺少 metadata.json: {recording_dir}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"metadata.json 读取失败: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"metadata.json 顶层必须是对象: {path}")
    return RecordingMetadata.from_dict(data)


# ---------------------------------------------------------------------------
# 录制核心（可注入截屏源/时钟/等待器，离线测试不截屏、不 sleep）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecordOptions:
    """录制参数。"""

    output_dir: Path
    capture_hz: float = 2.0
    duration_s: float | None = 60.0
    max_frames: int | None = None
    downscale: float = 2.0
    roi: Rect | None = None
    state_labels: bool = True
    stop_key: str = DEFAULT_STOP_KEY
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.capture_hz <= 0:
            raise ValueError(f"录制频率必须大于 0: {self.capture_hz!r}")
        if self.duration_s is not None and self.duration_s <= 0:
            raise ValueError(f"录制时长必须大于 0: {self.duration_s!r}")
        if self.max_frames is not None and self.max_frames <= 0:
            raise ValueError(f"最大帧数必须大于 0: {self.max_frames!r}")
        if self.duration_s is None and self.max_frames is None:
            raise ValueError("duration_s 与 max_frames 至少要给出一个限制")
        resolve_downscale(self.downscale)


def _new_metadata(
    options: RecordOptions,
    *,
    profile_id: str,
    client_id: str,
    reference_resolution: tuple[int, int],
    created_at: str,
) -> RecordingMetadata:
    roi_values = None
    if options.roi is not None:
        roi_values = [
            options.roi.x,
            options.roi.y,
            options.roi.right,
            options.roi.bottom,
        ]
    return RecordingMetadata(
        created_at=created_at,
        profile_id=profile_id,
        client_id=client_id,
        reference_resolution=reference_resolution,
        capture_hz=float(options.capture_hz),
        downscale=float(options.downscale),
        roi=roi_values,
        notes=list(options.notes),
    )


def record_frames(
    capture: FrameCapture,
    options: RecordOptions,
    *,
    clock: Clock | None = None,
    sleeper: Sleeper | None = None,
    label_source: FrameLabelSource | None = None,
    stop_source: StopSource | None = None,
    profile_id: str = "cn360",
    client_id: str = "",
    reference_resolution: tuple[int, int] = (0, 0),
    on_frame: Callable[[FrameRecord, int], None] | None = None,
) -> RecordingMetadata:
    """按固定频率录制，返回已写入磁盘的 metadata。

    ``clock`` 提供单调时间（控制时长），``sleeper`` 负责按频率等待；两者都可注入
    fake 实现，单元测试不真正截屏、不真正等待。
    """
    resolved_clock = clock or SystemClock()
    resolved_sleeper = sleeper or SystemSleeper()
    output_dir = Path(options.output_dir)
    frames_dir = output_dir / FRAMES_DIRNAME
    frames_dir.mkdir(parents=True, exist_ok=True)
    metadata = _new_metadata(
        options,
        profile_id=profile_id,
        client_id=client_id,
        reference_resolution=reference_resolution,
        created_at=iso_timestamp(resolved_clock.wall_now()),
    )
    interval = 1.0 / float(options.capture_hz)
    started_at = resolved_clock.now()
    next_capture_at = started_at
    consecutive_failures = 0
    while True:
        if stop_source is not None and stop_source.should_stop():
            metadata.notes.append(f"收到停止信号，已录制 {metadata.frame_count} 帧")
            break
        if options.max_frames is not None and metadata.frame_count >= options.max_frames:
            break
        now = resolved_clock.now()
        if options.duration_s is not None and now - started_at >= options.duration_s:
            break
        if now < next_capture_at:
            resolved_sleeper.sleep(min(interval, next_capture_at - now))
            continue
        try:
            frame = capture.capture()
        except CaptureUnavailable as exc:
            consecutive_failures += 1
            logger.warning("录制截屏失败（第 %d 次）: %s", consecutive_failures, exc)
            if consecutive_failures >= MAX_CONSECUTIVE_CAPTURE_FAILURES:
                metadata.notes.append(
                    f"连续截屏失败 {consecutive_failures} 次，录制提前结束: {exc}"
                )
                break
            next_capture_at = resolved_clock.now() + interval
            continue
        consecutive_failures = 0
        try:
            processed = prepare_frame_for_recording(
                frame.image,
                downscale=options.downscale,
                roi=options.roi,
            )
        except ValueError as exc:
            metadata.notes.append(f"帧 {frame.frame_id} 预处理失败，已跳过: {exc}")
            next_capture_at = resolved_clock.now() + interval
            continue
        frame_id = metadata.frame_count + 1
        relative_file = f"{FRAMES_DIRNAME}/{frame_id:06d}.png"
        write_image(output_dir / relative_file, processed)
        height, width = processed.shape[:2]
        if metadata.frame_size == (0, 0):
            metadata.frame_size = (int(width), int(height))
            metadata.window_rect = frame.window_rect.to_tuple()
        record = FrameRecord(
            frame_id=frame_id,
            file=relative_file,
            timestamp=float(frame.timestamp),
            window_rect=frame.window_rect.to_tuple(),
        )
        metadata.frames.append(record)
        if options.state_labels and label_source is not None:
            label = label_source.poll()
            if label:
                metadata.labels.append(
                    {"frame_id": frame_id, "state": label, "ts": round(float(frame.timestamp), 6)}
                )
        if on_frame is not None:
            on_frame(record, frame.frame_id)
        # 按固定节拍推进；截屏耗时超过周期时立即恢复节拍，不累积延迟
        next_capture_at += interval
        if next_capture_at < resolved_clock.now():
            next_capture_at = resolved_clock.now()
    metadata.labels.sort(key=lambda item: int(item.get("frame_id", 0)))
    write_metadata(output_dir, metadata)
    return metadata


# ---------------------------------------------------------------------------
# 热键标签/停止（installed 使用；单元测试注入假实现）
# ---------------------------------------------------------------------------


class KeyboardLabelSource:
    """轮询 F1~F8 产生状态标签（按下沿触发一次；F12 同时作为停止键）。"""

    def __init__(
        self,
        *,
        key_state: Any = None,
        hotkeys: dict[str, str] | None = None,
        stop_key: str = DEFAULT_STOP_KEY,
    ) -> None:
        if key_state is None:
            from ..execution.safety import Win32KeyStateSource

            key_state = Win32KeyStateSource()
        self._key_state = key_state
        self._hotkeys = dict(hotkeys if hotkeys is not None else DEFAULT_STATE_HOTKEYS)
        self._stop_key = stop_key
        self._down: set[str] = set()
        self._stop_seen = False

    @property
    def hotkey_help(self) -> str:
        mapping = "  ".join(f"{key.upper()}={state}" for key, state in self._hotkeys.items())
        return f"{mapping}  {self._stop_key.upper()}=停止"

    def poll(self) -> str | None:
        """返回新触发的状态标签；同一按键保持按下只触发一次。"""
        label: str | None = None
        for key, state in self._hotkeys.items():
            down = self._is_down(key)
            if down and key not in self._down and label is None:
                label = state
            if down:
                self._down.add(key)
            else:
                self._down.discard(key)
        return label

    def is_stop_pressed(self) -> bool:
        if self._is_down(self._stop_key):
            if not self._stop_seen:
                self._stop_seen = True
                return True
            return False
        self._stop_seen = False
        return False

    def should_stop(self) -> bool:
        return self.is_stop_pressed()

    def _is_down(self, key: str) -> bool:
        try:
            return bool(self._key_state.is_down(key))
        except Exception:  # noqa: BLE001 - 读取失败时视为未按下，不中断录制
            return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _default_output_dir(root: Path) -> Path:
    return root / DEFAULT_OUTPUT_ROOT / datetime.now().strftime("%Y%m%d_%H%M%S")


def _parse_roi(text: str | None) -> tuple[float, float, float, float] | None:
    """解析归一化 ROI 参数；非法时抛 ``ValueError``。"""
    if not text:
        return None
    from .calibrate_common import parse_number_quad

    x1, y1, x2, y2 = parse_number_quad(text)
    if not (0.0 <= x1 < x2 <= 1.0 and 0.0 <= y1 < y2 <= 1.0):
        raise ValueError(f"--roi 需要 4 个 0~1 的归一化坐标且 x1<x2、y1<y2: {text!r}")
    return x1, y1, x2, y2


class _NormalizedRoi:
    """把归一化 ROI 换算成录制帧像素矩形（首帧尺寸确定后生效）。"""

    def __init__(self, values: tuple[float, float, float, float]) -> None:
        self.values = values
        self.rect: Rect | None = None

    def update(self, frame_width: int, frame_height: int) -> None:
        x1, y1, x2, y2 = self.values
        left = int(round(x1 * frame_width))
        top = int(round(y1 * frame_height))
        right = int(round(x2 * frame_width))
        bottom = int(round(y2 * frame_height))
        self.rect = Rect(left, top, max(1, right - left), max(1, bottom - top))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.tools.record",
        description="按固定频率录制游戏窗口画面，供 replay 离线回放与阈值调优",
    )
    parser.add_argument("--root", default=".", help="项目根目录（包含 config.yaml），默认当前目录")
    parser.add_argument("--profile", default="cn360", help="客户端配置编号，默认 cn360")
    parser.add_argument("--output", default=None, help="录制目录（默认 recordings/<时间戳>）")
    parser.add_argument("--duration", type=float, default=60.0, help="录制时长秒数（默认 60）")
    parser.add_argument("--max-frames", type=int, default=None, help="最大帧数（优先于时长限制）")
    parser.add_argument("--capture-hz", type=float, default=None, help="录制频率（默认取 runtime.capture_hz）")
    parser.add_argument("--downscale", type=float, default=2.0, help="降采样倍数，1=原尺寸（默认 2）")
    parser.add_argument(
        "--roi",
        default=None,
        help="只录归一化区域，例如 0.6,0.0,1.0,0.3（默认录整窗）",
    )
    parser.add_argument("--no-state-labels", action="store_true", help="不监听 F1~F8 状态标签热键")
    parser.add_argument("--no-stop-key", action="store_true", help="不监听 F12 停止热键（用 Ctrl+C 停止）")
    return parser


def main(argv: list[str] | None = None) -> int:
    """录制入口：加载窗口配置 -> 循环截屏 -> 写 PNG 与 metadata.json。"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _build_parser().parse_args(argv)
    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        print(f"[ERROR] 项目根目录不存在: {root}", file=sys.stderr)
        return 2
    try:
        project = load_project(root, args.profile)
    except Exception as exc:  # noqa: BLE001 - 配置异常给出清晰错误
        print(f"[ERROR] 配置加载失败: {exc}", file=sys.stderr)
        return 2
    config = project.config
    window = config.profile.window
    if not window.process_names and not window.title_patterns:
        print("[ERROR] profile.window 未配置进程名/窗口标题，无法定位游戏窗口", file=sys.stderr)
        return 2
    from ..common.window import WindowLocator

    locator = WindowLocator(window.process_names, window.title_patterns)
    info = locator.find()
    if info is None:
        print(
            "[ERROR] 找不到游戏窗口。请确认游戏已启动、profile.window 配置正确；"
            "录制工具不支持离线合成画面（离线回放可用 tests 合成数据）",
            file=sys.stderr,
        )
        return 2
    capture_hz = float(args.capture_hz or config.global_config.runtime.capture_hz)
    if capture_hz <= 0:
        print(f"[ERROR] 录制频率非法: {capture_hz}", file=sys.stderr)
        return 2
    try:
        downscale = resolve_downscale(args.downscale)
        roi_values = _parse_roi(args.roi)
    except ValueError as exc:
        print(f"[ERROR] 参数非法: {exc}", file=sys.stderr)
        return 2
    output_dir = Path(args.output).expanduser() if args.output else _default_output_dir(root)
    if not output_dir.is_absolute():
        output_dir = root / output_dir
    if output_dir.exists() and any(output_dir.iterdir()):
        print(f"[ERROR] 输出目录已存在且非空，避免覆盖: {output_dir}", file=sys.stderr)
        return 2

    capture = GdiCapture(locator, require_foreground=False)
    options = RecordOptions(
        output_dir=output_dir,
        capture_hz=capture_hz,
        duration_s=None if args.max_frames is not None else float(args.duration),
        max_frames=args.max_frames,
        downscale=downscale,
        state_labels=not args.no_state_labels,
        notes=(f"窗口 hwnd={info.hwnd} 客户区={info.client_rect.w}x{info.client_rect.h}",),
    )

    state_labels_enabled = not args.no_state_labels
    stop_enabled = not args.no_stop_key
    label_source = None
    stop_source = None
    roi_normalized = _NormalizedRoi(roi_values) if roi_values else None

    if state_labels_enabled or stop_enabled:
        try:
            keyboard = KeyboardLabelSource(stop_key=DEFAULT_STOP_KEY)
        except Exception as exc:  # noqa: BLE001 - 键盘监听不可用时降级
            print(f"[WARN] 键盘热键不可用（{exc}），状态标签与 F12 停止被禁用", file=sys.stderr)
            state_labels_enabled = False
            stop_enabled = False
        else:
            label_source = keyboard if state_labels_enabled else None
            stop_source = keyboard if stop_enabled else None
            print(f"状态标签热键: {keyboard.hotkey_help}")

    if roi_normalized is not None:
        # 先截一帧确定尺寸，再换算 ROI；随后录制从该帧之后开始
        try:
            probe = capture.capture()
        except CaptureUnavailable as exc:
            print(f"[ERROR] 首帧截屏失败，无法确定 ROI: {exc}", file=sys.stderr)
            capture.close()
            return 2
        roi_normalized.update(probe.width, probe.height)
    options_with_roi = RecordOptions(
        output_dir=options.output_dir,
        capture_hz=options.capture_hz,
        duration_s=options.duration_s,
        max_frames=options.max_frames,
        downscale=options.downscale,
        roi=roi_normalized.rect if roi_normalized is not None else None,
        state_labels=state_labels_enabled,
        notes=options.notes,
    )
    print(
        f"开始录制: 输出={output_dir} 频率={capture_hz:g}Hz "
        f"降采样={downscale:g} 时长={options.duration_s} 最大帧数={options.max_frames}"
    )
    print("录制期间请让游戏窗口保持无边框窗口模式；按 F12（或 Ctrl+C）停止。")
    try:
        metadata = record_frames(
            capture,
            options_with_roi,
            label_source=label_source,
            stop_source=stop_source,
            profile_id=args.profile,
            client_id=config.profile.client_id,
            reference_resolution=config.profile.reference_resolution,
            on_frame=lambda record, _source_id: print(
                f"  已录制 {record.frame_id} 帧", end="\r", flush=True
            ),
        )
    except KeyboardInterrupt:
        print("\n[INFO] 用户中断录制")
        capture.close()
        return 2
    except Exception as exc:  # noqa: BLE001 - 录制失败给出可读错误
        print(f"\n[ERROR] 录制失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        capture.close()
        return 2
    finally:
        capture.close()
    print()
    print(f"录制完成: {metadata.frame_count} 帧，状态标签 {len(metadata.labels)} 个")
    print(f"metadata: {output_dir / METADATA_FILENAME}")
    for note in metadata.notes:
        print(f"  备注: {note}")
    return 0 if metadata.frame_count > 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
