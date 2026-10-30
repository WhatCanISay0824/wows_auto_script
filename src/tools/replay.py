"""录制回放工具（详细设计 12.4）。

读取 ``record.py`` 生成的录制目录，用与运行时完全相同的感知代码
（``TemplateMatcher`` / ``FeatureMatcher`` / ``MapIdentifier`` / ``ShipLocator``）
逐帧识别，不发送任何键鼠事件。输出：

- 识别状态分布（UNKNOWN 表示没有命中任何已知状态）；
- 每个界面特征的命中帧数、平均/最高匹配得分；
- 地图识别结果分布（地图编号 / MISS / SKIP）与失败帧数；
- 自身舰船定位来源分布；
- 与 ``metadata.json`` 中人工状态标签的比对准确率；
- 异常与错误统计（单帧识别异常不会中断回放）。

录制时窗口尺寸变化或 ROI 裁剪会让归一化区域语义变化，回放结果不可直接比较；
这类情况会在报告备注里提示。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2

from ..common.coords import expected_minimap_scale
from ..common.types import MatchResult, Rect, State
from ..config.loader import load_project
from ..config.models import AppConfig
from ..config.validate import RUNTIME_FEATURES
from ..perception.capture import Frame, crop_bgr
from ..perception.features import FeatureMatcher
from ..perception.map_identify import MapIdentifier
from ..perception.templates import TemplateMatcher
from .calibrate_common import read_image, write_image
from .record import RecordingMetadata, load_metadata

#: UNKNOWN / MISS / SKIP 等聚合桶名称
UNKNOWN_STATE = "UNKNOWN"
MAP_MISS = "MISS"
MAP_SKIP = "SKIP"
SHIP_NONE = "NONE"
SHIP_SKIP = "SKIP"


# ---------------------------------------------------------------------------
# 识别器装配
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Recognizers:
    """一次回放使用的感知组件集合（未配置的组件为 None）。"""

    feature_ids: tuple[str, ...]
    feature_matcher: FeatureMatcher
    template_matcher: TemplateMatcher
    map_identifier: MapIdentifier | None = None
    expected_scale: float | None = None
    ship_locator: Any = None
    minimap_rect_px: Rect | None = None
    notes: tuple[str, ...] = ()

    @property
    def maps_enabled(self) -> bool:
        return self.map_identifier is not None

    @property
    def ship_enabled(self) -> bool:
        return self.ship_locator is not None


def _minimap_pixel_rect(config: AppConfig, frame_size: tuple[int, int]) -> Rect | None:
    """按录制帧尺寸把 profile.minimap.rect 换算成内容区像素矩形（裁掉边框）。"""
    minimap = config.profile.minimap
    if minimap is None:
        return None
    width, height = frame_size
    if width <= 0 or height <= 0:
        return None
    pixel_rect = minimap.rect.to_pixels(width, height)
    border = max(0, int(minimap.border_px))
    content = Rect(
        pixel_rect.x + border,
        pixel_rect.y + border,
        max(1, pixel_rect.w - 2 * border),
        max(1, pixel_rect.h - 2 * border),
    )
    if content.right > width or content.bottom > height or content.w <= 0 or content.h <= 0:
        return None
    return content


def _build_ship_locator(config: AppConfig) -> tuple[Any, str | None]:
    """尝试创建 ShipLocator；标记模板缺失时返回 (None, 原因)。"""
    minimap = config.profile.minimap
    if minimap is None or not minimap.marker_template:
        return None, "未配置自身标记模板，跳过舰船定位"
    profile_dir = config.profile.profile_dir
    marker_path = Path(minimap.marker_template)
    if not marker_path.is_absolute() and profile_dir is not None:
        marker_path = profile_dir / marker_path
    if not marker_path.is_file():
        return None, f"自身标记模板缺失（待标定）: {marker_path}"
    try:
        from ..perception.ship_locate import ShipLocator

        return ShipLocator(config.profile, config.global_config.perception), None
    except (ValueError, FileNotFoundError) as exc:
        return None, f"ShipLocator 初始化失败: {exc}"


def build_recognizers(config: AppConfig, metadata: RecordingMetadata, *, root: Path) -> Recognizers:
    """按配置与录制 metadata 装配感知组件（缺模板/缺地图时降级并记录原因）。"""
    notes: list[str] = []
    perception_cfg = config.global_config.perception
    template_matcher = TemplateMatcher(perception_cfg, project_root=root)
    issues = template_matcher.load_profile(config.profile)
    load_failures = [issue for issue in issues if issue.is_error]
    if load_failures:
        notes.append(f"{len(load_failures)} 个界面模板加载失败（待标定），对应特征不会命中")
    feature_matcher = FeatureMatcher(
        config.profile,
        perception_cfg,
        template_matcher=template_matcher,
        project_root=root,
    )

    map_identifier = None
    expected_scale = None
    if not config.maps:
        notes.append("没有地图包，跳过地图识别")
    elif config.profile.minimap is None:
        notes.append("未配置小地图区域，跳过地图识别")
    else:
        map_identifier = MapIdentifier(config.maps, perception_cfg)
        global_expected = config.global_config.map.expected_minimap_scale
        if global_expected is not None:
            expected_scale = float(global_expected)
        elif len(config.maps) == 1:
            minimap_rect = _minimap_pixel_rect(config, metadata.frame_size)
            if minimap_rect is not None:
                pack = next(iter(config.maps.values()))
                expected_scale = expected_minimap_scale(minimap_rect, pack.transform())
        if expected_scale is None:
            notes.append("未能推导期望小地图缩放（多地图且未配置 expected_minimap_scale），地图识别跳过缩放校验")
        if metadata.roi:
            notes.append("该录制使用了 ROI 裁剪，地图识别/特征区域按录制帧坐标解释，结果仅供粗略参考")

    ship_locator, ship_note = _build_ship_locator(config)
    if ship_note:
        notes.append(ship_note)

    feature_ids = template_matcher.feature_ids()
    known = set(feature_ids)
    runtime_only = set(RUNTIME_FEATURES)
    missing = sorted(runtime_only - known)
    if missing:
        notes.append(f"运行时注册特征未标定: {', '.join(missing)}")
    if metadata.frame_size[0] <= 0 or metadata.frame_size[1] <= 0:
        notes.append("录制 metadata 缺少帧尺寸信息，地图/小地图裁剪不可用")
    return Recognizers(
        feature_ids=feature_ids,
        feature_matcher=feature_matcher,
        template_matcher=template_matcher,
        map_identifier=map_identifier,
        expected_scale=expected_scale,
        ship_locator=ship_locator,
        minimap_rect_px=_minimap_pixel_rect(config, metadata.frame_size),
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# 回放统计
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FeatureScore:
    """单个界面特征在整段录制上的统计。"""

    feature_id: str
    evaluated: int = 0
    hits: int = 0
    mean_score: float = 0.0
    max_score: float = 0.0

    @property
    def hit_rate(self) -> float:
        return self.hits / self.evaluated if self.evaluated else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature_id": self.feature_id,
            "evaluated": self.evaluated,
            "hits": self.hits,
            "hit_rate": round(self.hit_rate, 4),
            "mean_score": round(self.mean_score, 4),
            "max_score": round(self.max_score, 4),
        }


@dataclass(slots=True)
class ReplayReport:
    """回放结果汇总。"""

    recording_dir: Path
    frames_total: int = 0
    frames_processed: int = 0
    frames_failed: int = 0
    average_frame_ms: float = 0.0
    state_counts: dict[str, int] = field(default_factory=dict)
    map_counts: dict[str, int] = field(default_factory=dict)
    ship_counts: dict[str, int] = field(default_factory=dict)
    features: dict[str, FeatureScore] = field(default_factory=dict)
    labeled_frames: int = 0
    correct_frames: int = 0
    failures_saved: int = 0
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def accuracy(self) -> float | None:
        if not self.labeled_frames:
            return None
        return self.correct_frames / self.labeled_frames

    def to_dict(self) -> dict[str, Any]:
        return {
            "recording_dir": str(self.recording_dir),
            "frames_total": self.frames_total,
            "frames_processed": self.frames_processed,
            "frames_failed": self.frames_failed,
            "average_frame_ms": round(self.average_frame_ms, 3),
            "state_counts": dict(self.state_counts),
            "map_counts": dict(self.map_counts),
            "ship_counts": dict(self.ship_counts),
            "features": {feature_id: item.to_dict() for feature_id, item in self.features.items()},
            "labeled_frames": self.labeled_frames,
            "correct_frames": self.correct_frames,
            "accuracy": None if self.accuracy is None else round(self.accuracy, 4),
            "failures_saved": self.failures_saved,
            "errors": list(self.errors),
            "notes": list(self.notes),
        }

    def format_lines(self) -> list[str]:
        lines = [
            "回放识别报告",
            f"录制目录: {self.recording_dir}",
            f"帧数: 共 {self.frames_total}，已处理 {self.frames_processed}，失败 {self.frames_failed}",
            f"平均每帧识别耗时: {self.average_frame_ms:.1f} ms",
            "",
            "识别状态分布:",
        ]
        if self.state_counts:
            for state, count in sorted(self.state_counts.items(), key=lambda item: (-item[1], item[0])):
                lines.append(f"  {state:<20} {count:>6}  ({count / max(1, self.frames_processed):.1%})")
        else:
            lines.append("  （无）")
        if self.map_counts:
            lines.append("")
            lines.append("地图识别结果:")
            for map_id, count in sorted(self.map_counts.items(), key=lambda item: (-item[1], item[0])):
                lines.append(f"  {map_id:<20} {count:>6}")
        if self.ship_counts:
            lines.append("")
            lines.append("舰船定位来源:")
            for source, count in sorted(self.ship_counts.items(), key=lambda item: (-item[1], item[0])):
                lines.append(f"  {source:<20} {count:>6}")
        if self.features:
            lines.append("")
            lines.append("特征命中统计（evaluated=评估帧数，hits=命中帧数）:")
            for feature_id, stats in sorted(self.features.items()):
                lines.append(
                    f"  {feature_id:<32} evaluated={stats.evaluated:>5} hits={stats.hits:>5} "
                    f"hit_rate={stats.hit_rate:.1%} mean={stats.mean_score:.3f} max={stats.max_score:.3f}"
                )
        lines.append("")
        if self.accuracy is None:
            lines.append("准确率: 录制没有人工状态标签，无法比对")
        else:
            lines.append(
                f"准确率: {self.accuracy:.1%}（{self.correct_frames}/{self.labeled_frames} 帧与人工标签一致）"
            )
        lines.append(f"不一致帧另存: {self.failures_saved} 张")
        if self.errors:
            lines.append("")
            lines.append(f"错误统计: {len(self.errors)} 条")
            for message in self.errors[:10]:
                lines.append(f"  {message}")
            if len(self.errors) > 10:
                lines.append(f"  …其余 {len(self.errors) - 10} 条见 JSON 报告")
        if self.notes:
            lines.append("")
            lines.append("备注:")
            for note in self.notes:
                lines.append(f"  {note}")
        return lines


# ---------------------------------------------------------------------------
# 回放主流程
# ---------------------------------------------------------------------------


def _crop_minimap(frame: Frame, minimap_rect: Rect | None) -> Any:
    if minimap_rect is None:
        return None
    try:
        return crop_bgr(frame.image, minimap_rect, copy=True)
    except ValueError:
        return None


def replay_frames(
    recording_dir: Path | str,
    metadata: RecordingMetadata,
    recognizers: Recognizers,
    *,
    max_frames: int | None = None,
    failures_dir: Path | str | None = None,
    notes: Sequence[str] = (),
    progress: Any = None,
) -> ReplayReport:
    """逐帧回放识别；单帧异常只记录不中断。不发送任何输入。"""
    directory = Path(recording_dir)
    report = ReplayReport(recording_dir=directory)
    report.errors = []
    report.notes = [*recognizers.notes, *metadata.notes, *notes]
    records = metadata.frames if max_frames is None else metadata.frames[: int(max_frames)]
    report.frames_total = len(metadata.frames)
    state_counts: Counter[str] = Counter()
    map_counts: Counter[str] = Counter()
    ship_counts: Counter[str] = Counter()
    feature_scores: dict[str, list[float]] = {feature_id: [] for feature_id in recognizers.feature_ids}
    feature_hits: Counter[str] = Counter()
    durations_ms: list[float] = []
    failure_path = Path(failures_dir) if failures_dir is not None else None
    upscale = max(1.0, float(metadata.downscale or 1.0))
    if upscale > 1.0:
        report.notes.append(
            f"录制时降采样 {upscale:g}x，回放已上采样回原尺寸以保持模板语义（画质有损，阈值结果仅供调参参考）"
        )

    for record in records:
        started = time.perf_counter()
        try:
            image = read_image(directory / record.file)
        except ValueError as exc:
            report.frames_failed += 1
            report.errors.append(f"帧 {record.frame_id} 读取失败: {exc}")
            continue
        if upscale > 1.0:
            height, width = image.shape[:2]
            image = cv2.resize(
                image,
                (max(1, int(round(width * upscale))), max(1, int(round(height * upscale)))),
                interpolation=cv2.INTER_LINEAR,
            )
        frame = Frame(
            image=image,
            timestamp=float(record.timestamp),
            window_rect=Rect(*record.window_rect),
            frame_id=int(record.frame_id),
        )
        try:
            recognizers.feature_matcher.set_frame(frame)
            results = recognizers.template_matcher.match_many(recognizers.feature_ids, frame)
            state = recognizers.feature_matcher.detect_state(frame)
            map_match = None
            if recognizers.map_identifier is not None:
                minimap_image = _crop_minimap(frame, recognizers.minimap_rect_px)
                if minimap_image is not None:
                    map_match = recognizers.map_identifier.identify(
                        minimap_image,
                        expected_scale=recognizers.expected_scale,
                        timestamp=float(record.timestamp),
                    )
            ship = None
            if recognizers.ship_locator is not None:
                minimap_image = _crop_minimap(frame, recognizers.minimap_rect_px)
                if minimap_image is not None:
                    ship = recognizers.ship_locator.locate(minimap_image, float(record.timestamp))
        except Exception as exc:  # noqa: BLE001 - 单帧异常不中断回放
            report.frames_failed += 1
            report.errors.append(f"帧 {record.frame_id} 识别异常: {type(exc).__name__}: {exc}")
            durations_ms.append((_perf_counter() - started) * 1000.0)
            continue
        durations_ms.append((_perf_counter() - started) * 1000.0)
        report.frames_processed += 1
        state_name = state.value if isinstance(state, State) else (str(state) if state else UNKNOWN_STATE)
        state_counts[state_name] += 1
        if recognizers.maps_enabled:
            if map_match is not None:
                map_counts[map_match.map_id] += 1
            else:
                map_counts[MAP_MISS] += 1
        else:
            map_counts[MAP_SKIP] += 1
        if recognizers.ship_enabled:
            ship_counts[ship.source if ship is not None else SHIP_NONE] += 1
        else:
            ship_counts[SHIP_SKIP] += 1
        for feature_id in recognizers.feature_ids:
            result: MatchResult | None = results.get(feature_id)
            if result is None:
                continue
            feature_scores[feature_id].append(result.score)
            if result.found:
                feature_hits[feature_id] += 1
        label = metadata.state_at(record.frame_id)
        if label:
            report.labeled_frames += 1
            if state_name == label:
                report.correct_frames += 1
            elif failure_path is not None:
                failure_path.mkdir(parents=True, exist_ok=True)
                target = failure_path / f"{record.frame_id:06d}_pred-{state_name}_label-{label}.png"
                try:
                    write_image(target, image)
                    report.failures_saved += 1
                except (OSError, ValueError) as exc:  # pragma: no cover - 取决于文件系统
                    report.errors.append(f"帧 {record.frame_id} 不一致帧保存失败: {exc}")
        if progress is not None:
            progress(record.frame_id, report.frames_processed)

    report.state_counts = dict(state_counts)
    report.map_counts = dict(map_counts)
    report.ship_counts = dict(ship_counts)
    report.features = {
        feature_id: FeatureScore(
            feature_id=feature_id,
            evaluated=len(scores),
            hits=int(feature_hits.get(feature_id, 0)),
            mean_score=(sum(scores) / len(scores)) if scores else 0.0,
            max_score=max(scores) if scores else 0.0,
        )
        for feature_id, scores in feature_scores.items()
    }
    report.average_frame_ms = (sum(durations_ms) / len(durations_ms)) if durations_ms else 0.0
    return report


def _perf_counter() -> float:
    import time

    return time.perf_counter()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.tools.replay",
        description="离线回放录制画面，用与运行时相同的感知代码输出识别结果与准确率（不发送输入）",
    )
    parser.add_argument("recording", help="录制目录（包含 metadata.json 与 frames/）")
    parser.add_argument("--root", default=".", help="项目根目录（包含 config.yaml），默认当前目录")
    parser.add_argument("--profile", default="cn360", help="客户端配置编号，默认 cn360")
    parser.add_argument("--ship", default=None, help="覆盖 config.yaml 的船只编号")
    parser.add_argument("--max-frames", type=int, default=None, help="只回放前 N 帧")
    parser.add_argument(
        "--save-failures",
        default=None,
        help="把与人工标签不一致的帧另存到该目录（默认不保存）",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出报告（便于工具链集成）")
    parser.add_argument("--output", default=None, help="把报告写入文件（默认只打印到控制台）")
    return parser


def main(argv: list[str] | None = None) -> int:
    """回放入口：读取录制 -> 构建感知组件 -> 逐帧识别 -> 打印/保存报告。"""
    args = _build_parser().parse_args(argv)
    recording_dir = Path(args.recording).expanduser().resolve()
    if not recording_dir.is_dir():
        print(f"[ERROR] 录制目录不存在: {recording_dir}", file=sys.stderr)
        return 2
    try:
        metadata = load_metadata(recording_dir)
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        print(f"[ERROR] 项目根目录不存在: {root}", file=sys.stderr)
        return 2
    try:
        project = load_project(root, args.profile, ship_id=args.ship)
    except Exception as exc:  # noqa: BLE001 - 配置异常给出清晰错误
        print(f"[ERROR] 配置加载失败: {exc}", file=sys.stderr)
        return 2
    config = project.config
    if args.max_frames is not None and args.max_frames <= 0:
        print(f"[ERROR] --max-frames 必须大于 0: {args.max_frames}", file=sys.stderr)
        return 2
    try:
        recognizers = build_recognizers(config, metadata, root=root)
    except Exception as exc:  # noqa: BLE001 - 感知组件装配失败时给出可读错误
        print(f"[ERROR] 感知组件装配失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(
        f"回放 {recording_dir}：{metadata.frame_count} 帧，"
        f"特征 {len(recognizers.feature_ids)} 个，地图识别={'开' if recognizers.maps_enabled else '关'}，"
        f"舰船定位={'开' if recognizers.ship_enabled else '关'}"
    )
    report = replay_frames(
        recording_dir,
        metadata,
        recognizers,
        max_frames=args.max_frames,
        failures_dir=args.save_failures,
    )
    text = (
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
        if args.json
        else "\n".join(report.format_lines())
    )
    print(text)
    if args.output:
        output_path = Path(args.output).expanduser()
        if not output_path.is_absolute():
            output_path = root / output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text + "\n", encoding="utf-8")
        print(f"\n报告已写入: {output_path}")
    return 0
if __name__ == "__main__":
    raise SystemExit(main())
