"""船只与空装备标定 GUI（详细设计 12.1 / 12.3 / 6.8）。

用法：::

    python -m src.tools.calibrate_ship --root . --profile cn360 --ship-id placeholder \
        --screenshot 港口截图.png

功能：

- 从港口截图框选船只卡片，保存为 ``ships/<ship_id>/card.png``；
- 框选“已选中状态”的目标船卡片（含选中高亮/边框），保存为 ``ships/<ship_id>/selected.png``
  （ISS-007：运行时用它确认“当前选中的就是目标船”，不能把卡片可见当作已选中）；
- 框选右侧详情面板的“当前已选中船名”文字，保存为 ``ships/<ship_id>/selected_name.png``
  （ISS-007 补充：卡片选中态受背景/高亮影响时，船名文字是更稳的已选中证据）；
- 框选空信号旗槽 / 空经济加成槽，保存为 ``ships/<ship_id>/loadout/<slot_id>_empty.png``
  （当前用户口径由 ``profile.ship_check_sequence`` 在装备页校验信号旗，空槽可留空保存）；
- 生成/更新 ``ship.yaml``（card_template、selected_feature/region/threshold、
  selected_name_feature/region/threshold、loadout_slots、滚动参数等；已有未知字段保留）；
- 模板尺寸/颜色方差提示（详细设计 12.3）。

GUI 与逻辑分离：加载、框选换算、质量检查、YAML 组装都是纯函数，单元测试不创建窗口。
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from . import calibrate_common as cc

#: 编号规则（与校验器一致）
SHIP_ID_PATTERN = re.compile(r"^[a-z0-9_]+$")
#: 船只卡片模板路径（相对船只目录）
CARD_TEMPLATE = "card.png"
#: 已选中态模板路径（相对船只目录；ISS-007：ShipChecker 用它确认目标船已被选中）
SELECTED_TEMPLATE = "selected.png"
#: 右侧详情面板“当前已选中船名”模板路径（ISS-007 补充：比卡片选中态更稳的已选中证据）
SELECTED_NAME_TEMPLATE = "selected_name.png"
#: 选中态/船名模板默认阈值（与 ship.yaml 模型默认值一致）
SELECTED_THRESHOLD = 0.78
#: 右侧船名模板默认搜索区域（归一化窗口客户区；实机 2560×1600 右上详情面板）
SELECTED_NAME_REGION: tuple[float, float, float, float] = (0.79, 0.07, 0.91, 0.13)
#: 默认空装备槽（详细设计 6.8）
DEFAULT_LOADOUT_SLOTS: tuple[tuple[str, str], ...] = (
    ("signal_flags", "信号旗槽位"),
    ("economic_bonus", "经济加成槽位"),
)
#: ship.yaml 规范字段顺序
KNOWN_SHIP_FIELDS: tuple[str, ...] = (
    "ship_id",
    "display_name",
    "card_template",
    "card_search_region",
    "selected_feature",
    "selected_region",
    "selected_threshold",
    "selected_name_feature",
    "selected_name_region",
    "selected_name_threshold",
    "loadout_preset_feature",
    "max_scrolls",
    "scroll_direction",
    "scroll_clicks",
    "loadout_slots",
    "notes",
)
SHIP_YAML_HEADER = """# 船只配置（详细设计 3.2 / 6.8 / ISS-007）
# 由 src/tools/calibrate_ship 生成/更新；模板相对本目录
# （card.png、selected.png、selected_name.png、loadout/*.png）。"""


# ---------------------------------------------------------------------------
# 编辑状态
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ShipEditorState:
    """船只标定界面的全部可编辑状态。"""

    ship_dir: Path
    ship_path: Path
    data: dict[str, Any]
    ship_id: str = ""
    display_name: str = ""
    screenshot_path: Path | None = None
    screenshot: np.ndarray | None = None
    card_image: np.ndarray | None = None
    selected_image: np.ndarray | None = None
    name_image: np.ndarray | None = None
    loadout_images: dict[str, np.ndarray] = field(default_factory=dict)
    load_messages: list[str] = field(default_factory=list)

    @property
    def frame_size(self) -> tuple[int, int]:
        if self.screenshot is None:
            return (0, 0)
        height, width = self.screenshot.shape[:2]
        return int(width), int(height)


def empty_template_rel(slot_id: str) -> str:
    """空装备槽模板的相对路径（相对船只目录）。"""
    return f"loadout/{slot_id}_empty.png"


def load_ship_editor(
    ship_dir: Path | str,
    screenshot_path: Path | str | None = None,
) -> ShipEditorState:
    """载入 ship.yaml 与截图；文件缺失/字段非法时用默认值并记录提示。"""
    directory = Path(ship_dir).expanduser()
    ship_path = directory / "ship.yaml"
    state = ShipEditorState(ship_dir=directory, ship_path=ship_path, data={})
    if ship_path.is_file():
        try:
            state.data = cc.read_yaml_mapping(ship_path)
        except ValueError as exc:
            state.load_messages.append(f"ship.yaml 读取失败，将按新配置处理: {exc}")
    else:
        state.load_messages.append(f"ship.yaml 不存在（保存时新建）: {ship_path}")
    state.ship_id = str(state.data.get("ship_id") or directory.name)
    state.display_name = str(state.data.get("display_name") or state.ship_id)
    if screenshot_path is not None:
        try:
            state.screenshot = cc.read_image(screenshot_path)
            state.screenshot_path = Path(screenshot_path).expanduser()
        except ValueError as exc:
            state.load_messages.append(f"截图读取失败: {exc}")
    return state


# ---------------------------------------------------------------------------
# 字段写回与校验（纯函数）
# ---------------------------------------------------------------------------


def set_ship_identity(data: dict[str, Any], ship_id: str, display_name: str) -> dict[str, Any]:
    """写回 ship_id / display_name。"""
    if not SHIP_ID_PATTERN.match(ship_id or ""):
        raise ValueError(f"船只编号应是小写字母/数字/下划线: {ship_id!r}")
    if not display_name.strip():
        raise ValueError("船只显示名称不能为空")
    data["ship_id"] = ship_id
    data["display_name"] = display_name.strip()
    return data


def _loadout_slots(data: dict[str, Any]) -> list[dict[str, Any]]:
    raw = data.get("loadout_slots")
    if not isinstance(raw, list):
        raw = []
        data["loadout_slots"] = raw
    return raw


def upsert_loadout_slot(
    data: dict[str, Any],
    slot_id: str,
    *,
    name: str,
    empty_template: str,
    region: list[float] | None = None,
) -> dict[str, Any]:
    """插入或更新一个空装备槽定义，返回同一字典。"""
    if not SHIP_ID_PATTERN.match(slot_id or ""):
        raise ValueError(f"槽位编号应是小写字母/数字/下划线: {slot_id!r}")
    slots = _loadout_slots(data)
    entry: dict[str, Any] = {
        "slot_id": slot_id,
        "name": name,
        "empty_template": empty_template,
    }
    if region is not None:
        entry["region"] = [round(float(value), 4) for value in region]
    for index, item in enumerate(slots):
        if isinstance(item, dict) and item.get("slot_id") == slot_id:
            merged = dict(item)
            merged.update(entry)
            slots[index] = merged
            return data
    slots.append(entry)
    return data


def ship_yaml_payload(state: ShipEditorState) -> dict[str, Any]:
    """按规范字段顺序组装 ship.yaml，保留未知字段。

    ISS-007：``selected_feature`` 指向 ``selected.png``；``selected_region`` 缺省沿用
    ``card_search_region``（选中态模板与船卡在同一轮播条区域搜索），阈值 0.85。
    本次未重新框选 selected.png 时保留 ship.yaml 里已有的 selected_* 字段，避免误删标定。

    ISS-007 补充：``selected_name_*`` 指向右侧详情面板船名模板 ``selected_name.png``，
    region 缺省 ``[0.79, 0.07, 0.91, 0.13]``。只在本次框选船名或 ship.yaml 已有该组字段时写入，
    避免从未标定过船名的新船 yaml 里出现空 name 字段；已有值在未重新框选时原样保留。
    """
    source = state.data
    card_search_region = source.get("card_search_region")
    # 本次框选 selected.png 时固定写 selected.png（与 save_ship_editor 写出的文件名一致）；
    # 未重新框选时保留 ship.yaml 里已有的 selected_feature，避免误删标定。
    selected_feature: Any = (
        SELECTED_TEMPLATE if state.selected_image is not None else source.get("selected_feature")
    )
    payload: dict[str, Any] = {
        "ship_id": state.ship_id,
        "display_name": state.display_name,
        "card_template": str(source.get("card_template") or CARD_TEMPLATE),
        "card_search_region": card_search_region,
        "selected_feature": selected_feature,
        "selected_region": source.get("selected_region") or card_search_region,
        "selected_threshold": source.get("selected_threshold", SELECTED_THRESHOLD),
    }
    name_configured = state.name_image is not None or any(
        key in source
        for key in ("selected_name_feature", "selected_name_region", "selected_name_threshold")
    )
    if name_configured:
        payload["selected_name_feature"] = (
            SELECTED_NAME_TEMPLATE if state.name_image is not None else source.get("selected_name_feature")
        )
        payload["selected_name_region"] = source.get("selected_name_region") or list(SELECTED_NAME_REGION)
        payload["selected_name_threshold"] = source.get("selected_name_threshold", SELECTED_THRESHOLD)
    payload.update(
        {
            "loadout_preset_feature": source.get("loadout_preset_feature"),
            "max_scrolls": source.get("max_scrolls", 20),
            "scroll_direction": source.get("scroll_direction", "down"),
            "scroll_clicks": source.get("scroll_clicks", 1),
            "loadout_slots": list(_loadout_slots(source)),
            "notes": str(source.get("notes") or ""),
        }
    )
    for key, value in source.items():
        if str(key) not in KNOWN_SHIP_FIELDS:
            payload.setdefault(str(key), value)
    return payload


def validate_ship_editor(state: ShipEditorState) -> list[str]:
    """保存前结构检查；返回中文错误列表（阻断保存）。

    与旧版的差异（ISS-007 收尾）：
    - 新增已选中态模板 ``selected.png`` 的质量检查；未框选不报错（向后兼容）；
    - 空 ``loadout_slots`` 不再阻断保存（当前用户口径：信号旗由
      ``profile.ship_check_sequence`` 在装备页校验），改由 :func:`ship_editor_warnings` 提示。
    - ISS-007 补充：右侧详情面板船名模板 ``selected_name.png`` 同样做质量检查，未框选不报错。
    """
    errors: list[str] = []
    if not state.ship_dir.is_dir():
        errors.append(f"船只目录不存在: {state.ship_dir}")
    if not SHIP_ID_PATTERN.match(state.ship_id or ""):
        errors.append(f"船只编号应是小写字母/数字/下划线: {state.ship_id!r}")
    if state.ship_dir.name != state.ship_id:
        errors.append(f"ship_id {state.ship_id!r} 与目录名 {state.ship_dir.name!r} 不一致")
    if not state.display_name.strip():
        errors.append("船只显示名称不能为空")
    if state.card_image is None:
        errors.append("尚未框选船只卡片模板")
    else:
        quality = cc.inspect_template(state.card_image)
        if not quality.ok:
            errors.extend(f"船只卡片: {message}" for message in quality.errors)
    if state.selected_image is not None:
        quality = cc.inspect_template(state.selected_image)
        if not quality.ok:
            errors.extend(f"选中态卡片: {message}" for message in quality.errors)
    if state.name_image is not None:
        quality = cc.inspect_template(state.name_image)
        if not quality.ok:
            errors.extend(f"右侧船名: {message}" for message in quality.errors)
    for slot_id, image in state.loadout_images.items():
        quality = cc.inspect_template(image)
        if not quality.ok:
            errors.extend(f"空装备槽 {slot_id}: {message}" for message in quality.errors)
    slot_ids = [str(item.get("slot_id")) for item in _loadout_slots(state.data) if isinstance(item, dict)]
    if len(slot_ids) != len(set(slot_ids)):
        errors.append("loadout_slots 的 slot_id 存在重复")
    return errors


def ship_editor_warnings(state: ShipEditorState) -> list[str]:
    """不阻断保存的提示（GUI/日志展示），与 :func:`validate_ship_editor` 的错误分开返回。"""
    warnings: list[str] = []
    slot_ids = [str(item.get("slot_id")) for item in _loadout_slots(state.data) if isinstance(item, dict)]
    if not slot_ids:
        warnings.append(
            "未配置空装备槽定义（可保存）：信号旗数量由 profile.ship_check_sequence 在装备页校验"
        )
    has_selected_file = (state.ship_dir / SELECTED_TEMPLATE).is_file()
    if state.selected_image is None and not has_selected_file:
        warnings.append(
            "尚未框选已选中态模板 selected.png：运行时会因无法确认“已选中”而保守失败，"
            "请用“框选已选中状态的卡片”再框选一次（含该船船名/船体与选中高亮，不能只截通用边框）"
        )
    return warnings


@dataclass(slots=True)
class ShipSaveResult:
    """船只配置保存结果。"""

    saved: bool
    files: tuple[Path, ...] = ()
    errors: tuple[str, ...] = ()


def save_ship_editor(state: ShipEditorState) -> ShipSaveResult:
    """保存卡片/已选中态/右侧船名/空槽 PNG 与 ship.yaml；结构错误时不写任何文件。"""
    errors = validate_ship_editor(state)
    if errors:
        return ShipSaveResult(saved=False, errors=tuple(errors))
    files: list[Path] = []
    assert state.card_image is not None
    card_path = state.ship_dir / CARD_TEMPLATE
    cc.write_image(card_path, state.card_image)
    files.append(card_path)
    if state.selected_image is not None:
        selected_path = state.ship_dir / SELECTED_TEMPLATE
        cc.write_image(selected_path, state.selected_image)
        files.append(selected_path)
    if state.name_image is not None:
        name_path = state.ship_dir / SELECTED_NAME_TEMPLATE
        cc.write_image(name_path, state.name_image)
        files.append(name_path)
    for slot_id, image in state.loadout_images.items():
        target = state.ship_dir / empty_template_rel(slot_id)
        cc.write_image(target, image)
        files.append(target)
    state.ship_dir.mkdir(parents=True, exist_ok=True)
    header = cc.read_leading_comments(state.ship_path) or SHIP_YAML_HEADER
    cc.write_yaml_mapping(state.ship_path, ship_yaml_payload(state), header=header)
    files.append(state.ship_path)
    state.loadout_images.clear()
    for warning in ship_editor_warnings(state):
        print(f"[保存提示] {warning}")
    return ShipSaveResult(saved=True, files=tuple(files))


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------


class ShipCalibrationApp:
    """船只标定主窗口（tkinter）；业务逻辑委托给本模块纯函数。"""

    def __init__(self, root: Any, state: ShipEditorState) -> None:
        tkinter = cc.import_tkinter()
        from tkinter import filedialog, messagebox  # noqa: PLC0415 - 延迟导入

        self._tk = tkinter
        self._filedialog = filedialog
        self._messagebox = messagebox
        self.root = root
        self.state = state
        self._pending: str | None = None
        self._pending_slot: tuple[str, str] = ("", "")
        self._build_ui()
        if state.screenshot is not None:
            self.canvas.set_image(state.screenshot)
        self._refresh_labels()

    def _build_ui(self) -> None:
        tk = self._tk
        main = tk.Frame(self.root)
        main.pack(fill="both", expand=True)
        left = tk.Frame(main, width=330)
        left.pack(side="left", fill="y", padx=6, pady=6)
        left.pack_propagate(False)
        tk.Label(left, text=f"船只目录: {self.state.ship_dir}", anchor="w", wraplength=310).pack(fill="x")
        tk.Label(left, text="船只编号", anchor="w").pack(fill="x")
        self._ship_id_var = tk.StringVar(value=self.state.ship_id)
        tk.Entry(left, textvariable=self._ship_id_var).pack(fill="x")
        tk.Label(left, text="显示名称", anchor="w").pack(fill="x")
        self._name_var = tk.StringVar(value=self.state.display_name)
        tk.Entry(left, textvariable=self._name_var).pack(fill="x", pady=(0, 6))
        self._info = tk.Label(left, text="", anchor="w", justify="left", fg="#0055aa", wraplength=310)
        self._info.pack(fill="x", pady=(0, 6))
        tk.Button(left, text="载入截图…", command=self._load_screenshot).pack(fill="x", pady=2)
        tk.Button(left, text="框选船只卡片（card.png）", command=lambda: self._start("card")).pack(fill="x", pady=2)
        tk.Button(
            left,
            text="框选已选中状态的卡片（selected.png）",
            command=lambda: self._start("selected"),
        ).pack(fill="x", pady=2)
        tk.Button(
            left,
            text="框选右侧详情面板船名（selected_name.png）",
            command=lambda: self._start("name"),
        ).pack(fill="x", pady=2)
        for slot_id, name in DEFAULT_LOADOUT_SLOTS:
            tk.Button(
                left,
                text=f"框选{name}（loadout/{slot_id}_empty.png）",
                command=lambda slot=slot_id, slot_name=name: self._start("slot", slot, slot_name),
            ).pack(fill="x", pady=2)
        tk.Button(left, text="保存船只配置", command=self._save).pack(fill="x", pady=(12, 2))
        tk.Button(left, text="校验标定结果", command=self._validate).pack(fill="x")

        right = tk.Frame(main)
        right.pack(side="left", fill="both", expand=True, padx=6, pady=6)
        self.canvas = cc.ImageCanvas(right, on_box=self._on_box_selected)
        self.canvas.frame.pack(fill="both", expand=True)
        self._hint = tk.Label(right, anchor="w", fg="#995500")
        self._hint.pack(fill="x")

    def _refresh_labels(self) -> None:
        card = "已框选" if self.state.card_image is not None else "未框选"
        if self.state.selected_image is not None:
            selected = "已框选"
        elif (self.state.ship_dir / SELECTED_TEMPLATE).is_file():
            selected = "已有文件"
        else:
            selected = "未框选"
        if self.state.name_image is not None:
            name = "已框选"
        elif (self.state.ship_dir / SELECTED_NAME_TEMPLATE).is_file():
            name = "已有文件"
        else:
            name = "未框选"
        slots = "、".join(sorted(self.state.loadout_images)) or "无"
        frame = (
            f"{self.state.frame_size[0]}x{self.state.frame_size[1]}" if self.state.screenshot is not None else "无"
        )
        saved_slots = [
            str(item.get("slot_id"))
            for item in self.state.data.get("loadout_slots", [])
            if isinstance(item, dict)
        ]
        self._info.configure(
            text=f"截图: {self.state.screenshot_path or '未载入'}（{frame}）\n"
            f"卡片: {card}\n已选中态: {selected}\n右侧船名: {name}\n本次框选空槽: {slots}\n"
            f"配置中槽位: {', '.join(saved_slots) or '无'}"
        )

    def _set_pending(self, pending: str | None, hint: str = "") -> None:
        self._pending = pending
        self.canvas.set_mode(cc.ImageCanvas.MODE_BOX if pending else cc.ImageCanvas.MODE_PAN)
        self._hint.configure(text=hint)

    def _load_screenshot(self) -> None:
        path = self._filedialog.askopenfilename(
            title="选择港口截图",
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

    def _start(self, mode: str, slot_id: str = "", slot_name: str = "") -> None:
        if self.state.screenshot is None:
            self._show_messages("缺少截图", ["请先点「载入截图…」或通过 --screenshot 指定"], error=True)
            return
        if mode == "card":
            self._set_pending("card", "拖动矩形框选船只卡片（包含卡片边框与名称，避免只截图标）")
        elif mode == "selected":
            self._set_pending(
                "selected",
                "拖动矩形框选“已选中状态”的目标船卡片：必须含该船船名/船体 + 选中高亮/边框，"
                "不能只截通用边框（否则任何船被选中都会误匹配）",
            )
        elif mode == "name":
            self._set_pending(
                "name",
                "拖动矩形框选右侧详情面板的“当前已选中船名”文字（指挥官上方）："
                "包含船名文字与周围固定背景，避免只截单个字；换船后该处文字会变化",
            )
        else:
            self._pending_slot = (slot_id, slot_name)
            self._set_pending("slot", f"拖动矩形框选空槽位 {slot_name} 的模板")

    def _on_box_selected(self, selection: cc.BoxSelection) -> None:
        pending = self._pending
        self._set_pending(None, "")
        if self.state.screenshot is None or pending is None:
            return
        try:
            template = cc.crop_region(self.state.screenshot, selection)
        except ValueError as exc:
            self._show_messages("模板裁剪失败", [str(exc)], error=True)
            return
        quality = cc.inspect_template(template)
        if not quality.ok:
            self._show_messages("模板质量不合格", list(quality.errors), error=True)
            return
        if pending == "card":
            self.state.card_image = template
            print(f"[船只卡片] {quality.summary()}")
        elif pending == "selected":
            self.state.selected_image = template
            print(f"[已选中态卡片] {quality.summary()}")
        elif pending == "name":
            self.state.name_image = template
            print(f"[右侧船名] {quality.summary()}")
        else:
            slot_id, slot_name = self._pending_slot
            width, height = self.state.frame_size
            region = cc.clamp_selection(
                selection.x, selection.y, selection.right, selection.bottom, width, height
            ).normalized(width, height)
            upsert_loadout_slot(
                self.state.data,
                slot_id,
                name=slot_name,
                empty_template=empty_template_rel(slot_id),
                region=region.to_list(),
            )
            self.state.loadout_images[slot_id] = template
            print(f"[空槽 {slot_id}] {quality.summary()}")
        self._refresh_labels()

    def _apply_identity(self) -> bool:
        try:
            set_ship_identity(self.state.data, self._ship_id_var.get().strip(), self._name_var.get())
        except ValueError as exc:
            self._show_messages("字段非法", [str(exc)], error=True)
            return False
        self.state.ship_id = self._ship_id_var.get().strip()
        self.state.display_name = self._name_var.get().strip()
        return True

    def _validate(self) -> None:
        if not self._apply_identity():
            return
        errors = validate_ship_editor(self.state)
        warnings = ship_editor_warnings(self.state)
        if errors:
            messages = [*errors, *(f"[提示] {message}" for message in warnings)]
            self._show_messages("船只标定校验", messages, error=True)
            return
        messages = ["标定结果检查通过", *(f"[提示] {message}" for message in warnings)]
        self._show_messages("船只标定校验", messages)

    def _save(self) -> None:
        if not self._apply_identity():
            return
        result = save_ship_editor(self.state)
        if not result.saved:
            self._show_messages("保存失败", list(result.errors), error=True)
            return
        messages = [f"已保存: {path}" for path in result.files]
        messages.extend(f"[提示] {message}" for message in ship_editor_warnings(self.state))
        self._show_messages("保存成功", messages)
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
        prog="python -m src.tools.calibrate_ship",
        description=(
            "船只标定 GUI：框选船只卡片、已选中态卡片（selected.png）、"
            "右侧详情面板船名（selected_name.png）与空装备槽模板，"
            "生成/更新 ships/<ship_id>/ship.yaml"
        ),
    )
    parser.add_argument("--root", default=".", help="项目根目录（包含 config.yaml），默认当前目录")
    parser.add_argument("--profile", default="cn360", help="客户端配置编号，默认 cn360")
    parser.add_argument("--ship-id", default="placeholder", help="船只编号（对应 ships/<ship_id>/），默认 placeholder")
    parser.add_argument("--ship-dir", default=None, help="直接指定船只目录（优先于 --root/--profile/--ship-id）")
    parser.add_argument(
        "--display-name",
        default=None,
        help="船只显示名称（例如 铁公爵）；不填则沿用 ship.yaml 或 ship_id，启动后也可在界面修改",
    )
    parser.add_argument("--screenshot", default=None, help="港口截图路径（启动后也可在界面载入）")
    return parser


def resolve_ship_dir(args: argparse.Namespace) -> Path:
    if args.ship_dir:
        directory = Path(args.ship_dir).expanduser()
        if not directory.is_absolute():
            directory = Path(args.root).expanduser().resolve() / directory
        return directory
    return Path(args.root).expanduser().resolve() / "profiles" / args.profile / "ships" / args.ship_id


def main(argv: list[str] | None = None) -> int:
    """船只标定入口：载入配置与截图并打开 GUI。"""
    args = _build_parser().parse_args(argv)
    ship_dir = resolve_ship_dir(args)
    # 新船首次标定时目录可能不存在：直接创建，方便“输入船名 → 标定一次”的流程
    ship_dir.mkdir(parents=True, exist_ok=True)
    screenshot = Path(args.screenshot).expanduser() if args.screenshot else None
    state = load_ship_editor(ship_dir, screenshot)
    if args.display_name:
        state.display_name = args.display_name.strip()
    try:
        root = cc.create_root(f"船只标定 - {ship_dir.name}")
    except RuntimeError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    ShipCalibrationApp(root, state)
    for message in state.load_messages:
        print(f"[载入提示] {message}")
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
