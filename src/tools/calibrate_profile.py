"""客户端配置标定 GUI（详细设计 12.1 / 12.3）。

用法：::

    python -m src.tools.calibrate_profile --root . --profile cn360 --screenshot 港口截图.png

功能：

- 载入一张实机截图（2560×1600 无边框窗口客户区）；
- 拖动矩形标出小地图内容区（``minimap.rect``，不含边框）与 ``minimap.border_px``；
- 拖动矩形标出己方血条区域（``battle_hud.hp_bar_region``）；
- 框选界面元素生成 ``features/<feature_id>.png`` 模板并写回 ``profile.yaml.features``，
  搜索区域按框选范围自动外扩，阈值可调；
- 模板尺寸/颜色方差检查（详细设计 12.3：模板过小或颜色单一容易误匹配，工具给出提示）；
- 保存 profile.yaml 时保留文件开头注释模板。

GUI 与逻辑分离：加载、区域换算、模板质量检查、字段写回都是纯函数，单元测试不创建窗口。
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..common.types import NormalizedRect
from . import calibrate_common as cc

#: 特征编号允许的字符（与校验器的 key 规则一致）
FEATURE_ID_PATTERN = re.compile(r"^[a-z0-9_]+$")
#: 模板搜索区域相对框选范围外扩比例（详细设计 12.3：模板包含周围固定背景）
DEFAULT_SEARCH_MARGIN = 0.1
DEFAULT_FEATURE_THRESHOLD = 0.85
#: 新建 profile 文件的注释模板
PROFILE_YAML_HEADER = """# 客户端配置：由 src/tools/calibrate_profile 生成/更新（详细设计 3.2 / 12.3）
# 小地图 rect 为内容区归一化坐标；features 的 template 相对本目录。
# 状态特征表、步骤序列等其余字段请手工维护或沿用已有配置。"""


# ---------------------------------------------------------------------------
# 编辑状态
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ProfileEditorState:
    """客户端标定界面的全部可编辑状态。"""

    profile_dir: Path
    profile_path: Path
    data: dict[str, Any]
    screenshot_path: Path | None = None
    screenshot: np.ndarray | None = None
    minimap_rect: NormalizedRect | None = None
    border_px: int = 0
    hp_region: NormalizedRect | None = None
    pending_templates: dict[str, np.ndarray] = field(default_factory=dict)
    load_messages: list[str] = field(default_factory=list)

    # --- 查询 ---------------------------------------------------------
    @property
    def frame_size(self) -> tuple[int, int]:
        if self.screenshot is None:
            return (0, 0)
        height, width = self.screenshot.shape[:2]
        return int(width), int(height)

    @property
    def features(self) -> dict[str, Any]:
        features = self.data.get("features")
        if not isinstance(features, dict):
            features = {}
            self.data["features"] = features
        return features


def _optional_normalized_rect(value: Any) -> NormalizedRect | None:
    if not isinstance(value, (list, tuple)):
        return None
    try:
        return NormalizedRect.from_list([float(item) for item in value])
    except (TypeError, ValueError):
        return None


def load_profile_editor(
    profile_path: Path | str,
    screenshot_path: Path | str | None = None,
) -> ProfileEditorState:
    """载入 profile.yaml（可不存在）与截图；不抛异常，问题记入 ``load_messages``。"""
    path = Path(profile_path).expanduser()
    state = ProfileEditorState(
        profile_dir=path.parent,
        profile_path=path,
        data={},
    )
    if path.is_file():
        try:
            state.data = cc.read_yaml_mapping(path)
        except ValueError as exc:
            state.load_messages.append(f"profile.yaml 读取失败，将按新配置处理: {exc}")
    else:
        state.load_messages.append(f"profile.yaml 不存在（保存时新建）: {path}")
    minimap = state.data.get("minimap")
    if isinstance(minimap, dict):
        state.minimap_rect = _optional_normalized_rect(minimap.get("rect"))
        border_value = minimap.get("border_px")
        if isinstance(border_value, int) and not isinstance(border_value, bool) and border_value >= 0:
            state.border_px = int(border_value)
        else:
            state.load_messages.append("minimap.border_px 非法的非负整数，已按 0 处理")
    battle_hud = state.data.get("battle_hud")
    if isinstance(battle_hud, dict):
        state.hp_region = _optional_normalized_rect(battle_hud.get("hp_bar_region"))
    if screenshot_path is not None:
        try:
            state.screenshot = cc.read_image(screenshot_path)
            state.screenshot_path = Path(screenshot_path).expanduser()
        except ValueError as exc:
            state.load_messages.append(f"截图读取失败: {exc}")
    return state


# ---------------------------------------------------------------------------
# 字段写回（纯函数，可单测）
# ---------------------------------------------------------------------------


def _ensure_section(data: dict[str, Any], key: str) -> dict[str, Any]:
    section = data.get(key)
    if not isinstance(section, dict):
        section = {}
        data[key] = section
    return section


def set_minimap_region(
    data: dict[str, Any], rect: NormalizedRect, *, border_px: int | None = None
) -> dict[str, Any]:
    """写回 minimap.rect 与（可选）minimap.border_px，返回同一字典。"""
    minimap = _ensure_section(data, "minimap")
    minimap["rect"] = [round(value, 4) for value in rect.to_list()]
    if border_px is not None:
        if border_px < 0:
            raise ValueError(f"border_px 不能为负: {border_px}")
        minimap["border_px"] = int(border_px)
    return data


def set_hp_bar_region(data: dict[str, Any], rect: NormalizedRect) -> dict[str, Any]:
    """写回 battle_hud.hp_bar_region。"""
    battle_hud = _ensure_section(data, "battle_hud")
    battle_hud["hp_bar_region"] = [round(value, 4) for value in rect.to_list()]
    return data


def feature_template_rel(feature_id: str) -> str:
    """界面特征模板的相对路径（相对 profile 目录）。"""
    return f"features/{feature_id}.png"


def search_region_for_box(
    box: cc.BoxSelection,
    image_width: int,
    image_height: int,
    *,
    margin_ratio: float = DEFAULT_SEARCH_MARGIN,
) -> NormalizedRect:
    """按框选范围外扩得到模板搜索区域（归一化，clamp 到画面内）。"""
    if box.w <= 0 or box.h <= 0:
        raise ValueError(f"框选区域为空: {box!r}")
    margin = max(0.0, float(margin_ratio))
    pad_x = max(1.0, box.w * margin)
    pad_y = max(1.0, box.h * margin)
    return cc.clamp_selection(
        box.x - pad_x,
        box.y - pad_y,
        box.right + pad_x,
        box.bottom + pad_y,
        image_width,
        image_height,
    ).normalized(image_width, image_height)


def add_feature(
    data: dict[str, Any],
    feature_id: str,
    *,
    template: str,
    region: NormalizedRect,
    threshold: float = DEFAULT_FEATURE_THRESHOLD,
) -> dict[str, Any]:
    """写回 features.<id>（区域已存在时整条替换），返回同一字典。"""
    if not FEATURE_ID_PATTERN.match(feature_id or ""):
        raise ValueError(f"特征编号应是小写字母/数字/下划线: {feature_id!r}")
    if not 0.0 < float(threshold) <= 1.0:
        raise ValueError(f"阈值必须落在 (0, 1]: {threshold!r}")
    features = _ensure_section(data, "features")
    features[feature_id] = {
        "template": template,
        "region": [round(value, 4) for value in region.to_list()],
        "threshold": round(float(threshold), 4),
    }
    return data


# ---------------------------------------------------------------------------
# 保存
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ProfileSaveResult:
    """profile 保存结果。"""

    saved: bool
    files: tuple[Path, ...] = ()
    errors: tuple[str, ...] = ()


def validate_editor_state(state: ProfileEditorState) -> list[str]:
    """保存前的结构检查（返回中文错误列表）。"""
    errors: list[str] = []
    profile_dir = state.profile_dir
    if not profile_dir.is_dir():
        errors.append(f"profile 目录不存在: {profile_dir}")
    if state.minimap_rect is None:
        errors.append("尚未标定小地图内容区 minimap.rect")
    if state.border_px < 0:
        errors.append(f"minimap.border_px 不能为负: {state.border_px}")
    if state.hp_region is None:
        errors.append("警告级：尚未标定血条区域 battle_hud.hp_bar_region（死亡判定会缺少血条信号）")
    for feature_id, image in state.pending_templates.items():
        if not FEATURE_ID_PATTERN.match(feature_id):
            errors.append(f"特征编号非法: {feature_id!r}")
            continue
        quality = cc.inspect_template(image)
        if not quality.ok:
            errors.extend(f"特征 {feature_id}: {message}" for message in quality.errors)
    return errors


def save_profile_editor(state: ProfileEditorState) -> ProfileSaveResult:
    """把待保存模板写成 PNG 并写回 profile.yaml；结构错误时不写任何文件。"""
    errors = [message for message in validate_editor_state(state) if not message.startswith("警告级")]
    if errors:
        return ProfileSaveResult(saved=False, errors=tuple(errors))
    files: list[Path] = []
    for feature_id, image in state.pending_templates.items():
        target = state.profile_dir / feature_template_rel(feature_id)
        cc.write_image(target, image)
        files.append(target)
    if state.minimap_rect is not None:
        set_minimap_region(state.data, state.minimap_rect, border_px=state.border_px)
    if state.hp_region is not None:
        set_hp_bar_region(state.data, state.hp_region)
    header = cc.read_leading_comments(state.profile_path) or PROFILE_YAML_HEADER
    state.profile_path.parent.mkdir(parents=True, exist_ok=True)
    cc.write_yaml_mapping(state.profile_path, state.data, header=header)
    files.append(state.profile_path)
    state.pending_templates.clear()
    return ProfileSaveResult(saved=True, files=tuple(files))


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------


class ProfileCalibrationApp:
    """客户端标定主窗口（tkinter）；业务逻辑全部委托给本模块纯函数。"""

    def __init__(self, root: Any, state: ProfileEditorState) -> None:
        tkinter = cc.import_tkinter()
        from tkinter import filedialog, messagebox  # noqa: PLC0415 - 延迟导入

        self._tk = tkinter
        self._filedialog = filedialog
        self._messagebox = messagebox
        self.root = root
        self.state = state
        self._pending: str | None = None
        self._build_ui()
        if state.screenshot is not None:
            self.canvas.set_image(state.screenshot)
        self._refresh_labels()

    # --- 界面 ---------------------------------------------------------
    def _build_ui(self) -> None:
        tk = self._tk
        main = tk.Frame(self.root)
        main.pack(fill="both", expand=True)
        left = tk.Frame(main, width=330)
        left.pack(side="left", fill="y", padx=6, pady=6)
        left.pack_propagate(False)
        tk.Label(left, text=f"客户端目录: {self.state.profile_dir}", anchor="w").pack(fill="x")
        self._info = tk.Label(left, text="", anchor="w", justify="left", fg="#0055aa")
        self._info.pack(fill="x", pady=(4, 8))

        tk.Button(left, text="载入截图…", command=self._load_screenshot).pack(fill="x", pady=2)
        self._border_var = tk.StringVar(value=str(self.state.border_px))
        tk.Label(left, text="小地图边框裁边 border_px", anchor="w").pack(fill="x")
        tk.Entry(left, textvariable=self._border_var).pack(fill="x", pady=(0, 4))
        tk.Button(left, text="标小地图内容区", command=self._start_minimap).pack(fill="x", pady=2)
        tk.Button(left, text="标己方血条区域", command=self._start_hp_region).pack(fill="x", pady=2)

        tk.Label(left, text="界面特征模板", anchor="w").pack(fill="x", pady=(10, 0))
        tk.Label(left, text="特征编号（小写字母/数字/下划线）", anchor="w").pack(fill="x")
        self._feature_id_var = tk.StringVar(value="port_indicator")
        tk.Entry(left, textvariable=self._feature_id_var).pack(fill="x")
        tk.Label(left, text="匹配阈值（0~1）", anchor="w").pack(fill="x")
        self._threshold_var = tk.StringVar(value=f"{DEFAULT_FEATURE_THRESHOLD:g}")
        tk.Entry(left, textvariable=self._threshold_var).pack(fill="x", pady=(0, 4))
        tk.Button(left, text="框选生成特征模板", command=self._start_feature).pack(fill="x", pady=2)
        self._feature_info = tk.Label(left, text="", anchor="w", justify="left", fg="#008855", wraplength=310)
        self._feature_info.pack(fill="x", pady=(4, 8))
        tk.Button(left, text="保存 profile.yaml", command=self._save).pack(fill="x", pady=(12, 2))
        tk.Button(left, text="校验标定结果", command=self._validate).pack(fill="x")

        right = tk.Frame(main)
        right.pack(side="left", fill="both", expand=True, padx=6, pady=6)
        self.canvas = cc.ImageCanvas(right, on_box=self._on_box_selected)
        self.canvas.frame.pack(fill="both", expand=True)
        self._hint = tk.Label(right, anchor="w", fg="#995500")
        self._hint.pack(fill="x")

    def _refresh_labels(self) -> None:
        minimap = cc.describe_normalized_rect(self.state.minimap_rect) if self.state.minimap_rect else "未标定"
        hp_bar = cc.describe_normalized_rect(self.state.hp_region) if self.state.hp_region else "未标定"
        frame = f"{self.state.frame_size[0]}x{self.state.frame_size[1]}" if self.state.screenshot is not None else "无"
        self._info.configure(
            text=f"截图: {self.state.screenshot_path or '未载入'}（{frame}）\n"
            f"小地图 rect: {minimap}\n"
            f"血条区域: {hp_bar}\n"
            f"已选特征模板数: {len(self.state.pending_templates)}"
        )

    def _set_pending(self, pending: str | None, hint: str = "") -> None:
        self._pending = pending
        self.canvas.set_mode(cc.ImageCanvas.MODE_BOX if pending else cc.ImageCanvas.MODE_PAN)
        self._hint.configure(text=hint)

    # --- 载入 ---------------------------------------------------------
    def _load_screenshot(self) -> None:
        path = self._filedialog.askopenfilename(
            title="选择实机截图",
            filetypes=[("图片", "*.png *.jpg *.jpeg *.bmp"), ("全部文件", "*.*")],
        )
        if not path:
            return
        try:
            self.state.screenshot = cc.read_image(path)
            self.state.screenshot_path = Path(path)
        except ValueError as exc:
            self._show_messages("截图读取失败", [str(exc)], error=True)
            return
        self.canvas.set_image(self.state.screenshot)
        self._refresh_labels()

    # --- 框选 ---------------------------------------------------------
    def _start_minimap(self) -> None:
        self._require_screenshot()
        self._set_pending("minimap", "拖动矩形标出小地图内容区（不含边框），松开后写回 minimap.rect")

    def _start_hp_region(self) -> None:
        self._require_screenshot()
        self._set_pending("hp_bar", "拖动矩形标出己方血条区域，松开后写回 battle_hud.hp_bar_region")

    def _start_feature(self) -> None:
        if not self._require_screenshot():
            return
        feature_id = self._feature_id_var.get().strip()
        if not FEATURE_ID_PATTERN.match(feature_id):
            self._show_messages("特征编号非法", ["应是小写字母/数字/下划线，例如 port_indicator"], error=True)
            return
        self._set_pending("feature", f"拖动矩形框选界面特征 {feature_id} 的模板（尽量包含周围固定背景）")

    def _require_screenshot(self) -> bool:
        if self.state.screenshot is None:
            self._show_messages("缺少截图", ["请先点「载入截图…」或通过 --screenshot 指定"], error=True)
            return False
        return True

    def _on_box_selected(self, selection: cc.BoxSelection) -> None:
        pending = self._pending
        self._set_pending(None, "")
        if self.state.screenshot is None or pending is None:
            return
        width, height = self.state.frame_size
        try:
            rect = selection.normalized(width, height)
        except ValueError as exc:
            self._show_messages("框选结果非法", [str(exc)], error=True)
            return
        if pending == "minimap":
            border = self._parse_border()
            if border is None:
                return
            self.state.minimap_rect = rect
            self.state.border_px = border
            set_minimap_region(self.state.data, rect, border_px=border)
            print(f"[小地图] rect={rect.to_list()} border_px={border}")
            self._refresh_labels()
        elif pending == "hp_bar":
            self.state.hp_region = rect
            set_hp_bar_region(self.state.data, rect)
            print(f"[血条] region={rect.to_list()}")
            self._refresh_labels()
        else:
            self._capture_feature(selection, rect)

    def _capture_feature(self, selection: cc.BoxSelection, rect: NormalizedRect) -> None:
        assert self.state.screenshot is not None
        try:
            template = cc.crop_region(self.state.screenshot, selection)
        except ValueError as exc:
            self._show_messages("模板裁剪失败", [str(exc)], error=True)
            return
        quality = cc.inspect_template(template)
        info_lines = [f"尺寸 {quality.summary()}"]
        info_lines.extend(quality.hints)
        if not quality.ok:
            self._show_messages("模板质量不合格", list(quality.errors), error=True)
            self._feature_info.configure(text="\n".join(info_lines))
            return
        feature_id = self._feature_id_var.get().strip()
        try:
            threshold = cc.parse_number(self._threshold_var.get())
        except ValueError:
            threshold = DEFAULT_FEATURE_THRESHOLD
        width, height = self.state.frame_size
        search_region = search_region_for_box(selection, width, height)
        try:
            add_feature(
                self.state.data,
                feature_id,
                template=feature_template_rel(feature_id),
                region=search_region,
                threshold=threshold,
            )
        except ValueError as exc:
            self._show_messages("特征字段非法", [str(exc)], error=True)
            return
        self.state.pending_templates[feature_id] = template
        lines = [
            f"特征 {feature_id} 已生成（尺寸 {quality.width}x{quality.height}，阈值 {threshold:g}）",
            f"搜索区域 region: {[round(value, 4) for value in search_region.to_list()]}",
            *quality.hints,
        ]
        self._feature_info.configure(text="\n".join(lines))
        self._refresh_labels()
        print("[特征] " + "\n".join(lines))

    def _parse_border(self) -> int | None:
        text = self._border_var.get().strip() or "0"
        try:
            value = cc.parse_number(text)
        except ValueError as exc:
            self._show_messages("border_px 非法", [str(exc)], error=True)
            return None
        if value < 0 or not float(value).is_integer():
            self._show_messages("border_px 非法", ["必须是非负整数"], error=True)
            return None
        return int(value)

    # --- 校验 / 保存 --------------------------------------------------
    def _validate(self) -> None:
        border = self._parse_border()
        if border is not None:
            self.state.border_px = border
        messages = validate_editor_state(self.state)
        if not messages:
            messages = ["标定结果检查通过"]
        self._show_messages("标定校验", messages, error=any(not m.startswith("警告级") for m in messages))

    def _save(self) -> None:
        border = self._parse_border()
        if border is not None:
            self.state.border_px = border
        result = save_profile_editor(self.state)
        if not result.saved:
            self._show_messages("保存失败", list(result.errors), error=True)
            return
        self._show_messages("保存成功", [f"已保存: {path}" for path in result.files])
        self._refresh_labels()

    def _show_messages(self, title: str, messages: list[str], *, error: bool = False) -> None:
        text = "\n".join(str(message) for message in messages)
        print(f"[{title}]\n{text}")
        if error:
            self._messagebox.showerror(title, text)
        else:
            self._messagebox.showinfo(title, text)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.tools.calibrate_profile",
        description="客户端标定 GUI：框选小地图/血条区域、生成界面特征模板并写回 profile.yaml",
    )
    parser.add_argument("--root", default=".", help="项目根目录（包含 config.yaml），默认当前目录")
    parser.add_argument("--profile", default="cn360", help="客户端配置编号，默认 cn360")
    parser.add_argument("--profile-dir", default=None, help="直接指定客户端配置目录（优先于 --root/--profile）")
    parser.add_argument("--screenshot", default=None, help="实机截图路径（启动后也可在界面载入）")
    return parser


def resolve_profile_dir(args: argparse.Namespace) -> Path:
    if args.profile_dir:
        directory = Path(args.profile_dir).expanduser()
        if not directory.is_absolute():
            directory = Path(args.root).expanduser().resolve() / directory
        return directory
    return Path(args.root).expanduser().resolve() / "profiles" / args.profile


def main(argv: list[str] | None = None) -> int:
    """客户端标定入口：载入配置与截图并打开 GUI。"""
    args = _build_parser().parse_args(argv)
    profile_dir = resolve_profile_dir(args)
    if not profile_dir.is_dir():
        print(f"[ERROR] 客户端配置目录不存在: {profile_dir}", file=sys.stderr)
        return 2
    screenshot = Path(args.screenshot).expanduser() if args.screenshot else None
    state = load_profile_editor(profile_dir / "profile.yaml", screenshot)
    try:
        root = cc.create_root(f"客户端标定 - {profile_dir.name}")
    except RuntimeError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    ProfileCalibrationApp(root, state)
    for message in state.load_messages:
        print(f"[载入提示] {message}")
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
