"""船卡导入（M2）：纯函数保存 + Tk 框选对话框（GUI 界面设计文档第 8 章）。

边界：

- 只保存裁剪后的船卡模板与最小 ``ship.yaml``，不复制完整港口截图（8.6 规则 4）；
- 允许 ``loadout_slots: []``（附录 D Q3），不改变 ``calibrate_ship`` 既有保存语义；
- **不静默覆盖**：目标目录已有档案时，必须显式选择“更新现有档案 / 另存新档案 / 取消”，
  保存函数在 ``overwrite=False`` 时直接拒绝；
- 质量不合格（过小/纯色）不允许保存，给出 ``inspect_template`` 的中文原因与建议；
- 纯函数（``list/suggest/save`` 等）不创建 Tk 窗口，可在无桌面环境下测试；
  对话框类的 ``import_tkinter()`` 在实例化时才导入 Tk。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from src.gui.viewmodel import ShipCandidate
from src.tools import calibrate_common as cc
from src.tools.calibrate_ship import CARD_TEMPLATE, SHIP_ID_PATTERN

SHIP_YAML_HEADER = """# 船只配置（详细设计 3.2 / 6.8）
# 由控制台“首次船卡导入”生成；模板相对本目录（card.png）。
# 信号旗校验由 profile.ship_check_sequence 完成；loadout_slots 允许为空。"""
DEFAULT_CARD_SEARCH_REGION: tuple[float, float, float, float] = (0.0, 0.80, 1.0, 1.0)
DEFAULT_NOTES = "由 GUI 首次导入生成；未实机验证识别效果。"


class ShipImportError(ValueError):
    """船卡导入/保存失败（中文原因，界面直接展示）。"""


@dataclass(frozen=True, slots=True)
class ShipSaveResult:
    ship_id: str
    display_name: str
    directory: Path
    card_path: Path
    yaml_path: Path
    updated: bool


def ships_root(project_root: Path, profile_id: str) -> Path:
    return Path(project_root) / "profiles" / profile_id / "ships"


def ship_directory(project_root: Path, profile_id: str, ship_id: str) -> Path:
    return ships_root(project_root, profile_id) / ship_id


def list_ship_candidates(project_root: Path, profile_id: str) -> list[ShipCandidate]:
    """列出本地已登记船只（读取每个 ``ships/*/ship.yaml`` 的 ship_id/display_name）。"""

    directory = ships_root(project_root, profile_id)
    if not directory.is_dir():
        return []
    candidates: list[ShipCandidate] = []
    for yaml_path in sorted(directory.glob("*/ship.yaml")):
        try:
            data = cc.read_yaml_mapping(yaml_path)
        except ValueError:
            continue
        ship_id = str(data.get("ship_id") or yaml_path.parent.name).strip()
        display_name = str(data.get("display_name") or ship_id).strip()
        if ship_id:
            candidates.append(ShipCandidate(ship_id=ship_id, display_name=display_name))
    return candidates


def _used_ship_ids(project_root: Path, profile_id: str) -> set[str]:
    directory = ships_root(project_root, profile_id)
    used = {item.ship_id for item in list_ship_candidates(project_root, profile_id)}
    if directory.is_dir():
        used.update(path.name for path in directory.iterdir() if path.is_dir())
    return used


def suggest_ship_id(project_root: Path, profile_id: str) -> str:
    """生成未占用的 ``ship_<3 位序号>``（满足 SHIP_ID_PATTERN）。"""

    used = _used_ship_ids(project_root, profile_id)
    for index in range(1, 1000):
        candidate = f"ship_{index:03d}"
        if candidate not in used:
            return candidate
    raise ShipImportError("无法生成新的船号（ship_001～ship_999 均已占用）")


def validate_ship_identity(ship_id: str, display_name: str) -> tuple[str, ...]:
    errors: list[str] = []
    if not SHIP_ID_PATTERN.match(ship_id or ""):
        errors.append(f"船号应是小写字母/数字/下划线: {ship_id!r}")
    if not (display_name or "").strip():
        errors.append("船名不能为空")
    return tuple(errors)


def display_name_conflicts(
    project_root: Path,
    profile_id: str,
    display_name: str,
    *,
    exclude_ship_id: str | None = None,
) -> tuple[ShipCandidate, ...]:
    """返回同名（display_name）的其它档案；``exclude_ship_id`` 用于“更新现有档案”。"""

    name = (display_name or "").strip()
    if not name:
        return ()
    return tuple(
        item
        for item in list_ship_candidates(project_root, profile_id)
        if item.display_name == name and item.ship_id != exclude_ship_id
    )


def build_ship_yaml(ship_id: str, display_name: str, *, notes: str = DEFAULT_NOTES) -> dict[str, object]:
    """最小 ``ship.yaml``（字段顺序参照 calibrate_ship.KNOWN_SHIP_FIELDS；允许空 loadout_slots）。"""

    return {
        "ship_id": ship_id,
        "display_name": display_name.strip(),
        "card_template": CARD_TEMPLATE,
        "card_search_region": list(DEFAULT_CARD_SEARCH_REGION),
        "loadout_slots": [],
        "max_scrolls": 20,
        "scroll_direction": "down",
        "scroll_clicks": 2,
        "notes": notes,
    }


def inspect_card(card_image: np.ndarray) -> cc.TemplateQuality:
    return cc.inspect_template(card_image)


def crop_card(image: np.ndarray, box: cc.BoxSelection) -> np.ndarray:
    return cc.crop_region(image, box)


def read_screenshot(path: Path | str) -> np.ndarray:
    return cc.read_image(path)


def save_ship_card(
    project_root: Path,
    profile_id: str,
    ship_id: str,
    display_name: str,
    card_image: np.ndarray,
    *,
    overwrite: bool = False,
) -> ShipSaveResult:
    """保存 ``card.png`` + 最小 ``ship.yaml``；不静默覆盖既有档案。"""

    display = (display_name or "").strip()
    errors = validate_ship_identity(ship_id, display)
    if errors:
        raise ShipImportError("；".join(errors))
    quality = cc.inspect_template(card_image)
    if not quality.ok:
        raise ShipImportError("模板质量不合格：" + "；".join(quality.errors))

    directory = ship_directory(project_root, profile_id, ship_id)
    card_path = directory / CARD_TEMPLATE
    yaml_path = directory / "ship.yaml"
    existed = card_path.is_file() or yaml_path.is_file()
    if existed and not overwrite:
        raise ShipImportError(
            f"船只档案已存在，未写入任何文件（不静默覆盖）：{directory}；"
            "如确认替换，请选择“更新现有档案”并二次确认"
        )

    payload = build_ship_yaml(ship_id, display)
    try:
        cc.write_image(card_path, card_image)
        cc.write_yaml_mapping(yaml_path, payload, header=SHIP_YAML_HEADER)
    except (OSError, ValueError) as exc:
        raise ShipImportError(f"保存失败：{exc}") from exc
    return ShipSaveResult(
        ship_id=ship_id,
        display_name=display,
        directory=directory,
        card_path=card_path,
        yaml_path=yaml_path,
        updated=existed,
    )


# ---------------------------------------------------------------------------
# Tk 框选对话框（只在实例化时导入 Tk；测试可用纯函数绕过）
# ---------------------------------------------------------------------------


class ShipImportDialog:
    """“导入船卡”对话框：选择本机图片 → ImageCanvas 框选 → 质量检查 → 命名 → 保存。"""

    def __init__(
        self,
        master,
        *,
        project_root: Path,
        profile_id: str,
        on_saved: Callable[[ShipSaveResult], None] | None = None,
        confirm_update: Callable[[ShipCandidate], bool] | None = None,
        close_on_save: bool = False,
    ) -> None:
        self._tk = cc.import_tkinter()
        self.project_root = Path(project_root)
        self.profile_id = profile_id
        self._on_saved = on_saved
        self._confirm_update = confirm_update or self._ask_update
        self._close_on_save = close_on_save

        self.top = self._tk.Toplevel(master)
        self.top.title("导入船卡（本机图片，不上传）")
        self.top.geometry("1040x760")
        self.top.transient(master)
        self._build_widgets()

        self._screenshot: np.ndarray | None = None
        self._card_image: np.ndarray | None = None
        self._quality: cc.TemplateQuality | None = None
        self._conflicts: tuple[ShipCandidate, ...] = ()
        self._override: str | None = None
        self._target_ship_id: str | None = None
        self._overwrite = False
        self._can_save = False
        self._status = "请选择一张港口截图（船卡在底部轮播条可见）。"
        self._refresh_status()

    # -- 构建 ---------------------------------------------------------
    def _build_widgets(self) -> None:
        tk = self._tk
        outer = tk.Frame(self.top, padx=10, pady=8)
        outer.pack(fill="both", expand=True)

        path_row = tk.Frame(outer)
        path_row.pack(fill="x")
        tk.Label(path_row, text="截图").pack(side="left")
        self.path_var = tk.StringVar(value="")
        tk.Entry(path_row, textvariable=self.path_var, state="readonly", width=70).pack(
            side="left", padx=(6, 6), fill="x", expand=True
        )
        tk.Button(path_row, text="选择图片…", command=self.choose_image).pack(side="left")

        canvas_row = tk.Frame(outer)
        canvas_row.pack(fill="both", expand=True, pady=(8, 4))
        self.canvas = cc.ImageCanvas(canvas_row, height=520, on_box=self._on_box_selected)
        self.canvas.frame.pack(fill="both", expand=True)

        hint = tk.Label(
            outer,
            text="只框船体剪影 + 船名，避开等级/+50% 徽章与选中边框；新船首次导入一次即可。",
            anchor="w",
            justify="left",
        )
        hint.pack(fill="x")

        quality_row = tk.Frame(outer)
        quality_row.pack(fill="x", pady=(4, 0))
        self.quality_label = tk.Label(quality_row, text="质量检查：—", anchor="w")
        self.quality_label.pack(side="left")

        name_row = tk.Frame(outer)
        name_row.pack(fill="x", pady=(6, 0))
        tk.Label(name_row, text="船名").pack(side="left")
        self.name_var = tk.StringVar(value="")
        self.name_var.trace_add("write", lambda *_args: self._refresh_conflicts())
        tk.Entry(name_row, textvariable=self.name_var, width=24).pack(side="left", padx=(6, 12))
        tk.Label(name_row, text="船号").pack(side="left")
        self.ship_id_var = tk.StringVar(value="")
        tk.Label(name_row, textvariable=self.ship_id_var, width=16, anchor="w").pack(side="left", padx=(6, 0))

        self.conflict_frame = tk.Frame(outer)
        self.conflict_label = tk.Label(self.conflict_frame, text="", anchor="w")
        self.conflict_label.pack(fill="x")
        buttons = tk.Frame(self.conflict_frame)
        buttons.pack(fill="x")
        tk.Button(buttons, text="更新现有档案", command=self.resolve_update).pack(side="left", padx=(0, 6))
        tk.Button(buttons, text="另存新档案", command=self.resolve_save_new).pack(side="left", padx=(0, 6))
        tk.Button(buttons, text="取消", command=self.cancel_conflict).pack(side="left")

        action_row = tk.Frame(outer)
        action_row.pack(fill="x", pady=(10, 0))
        self.status_label = tk.Label(action_row, text="", anchor="w", justify="left", wraplength=700)
        self.status_label.pack(side="left", fill="x", expand=True)
        self.save_button = tk.Button(action_row, text="保存并使用", command=self.save)
        self.save_button.pack(side="right", padx=(6, 0))
        tk.Button(action_row, text="关闭", command=self.close).pack(side="right")

    # -- 图像与框选 ---------------------------------------------------
    def load_image_array(self, image: np.ndarray) -> None:
        self._screenshot = image
        self.path_var.set("（内存图像）")
        self.canvas.set_image(image)
        self.canvas.set_mode(cc.ImageCanvas.MODE_BOX)
        self._set_status("已载入图片：请拖动框选船卡区域。")

    def load_screenshot(self, path: Path | str) -> np.ndarray:
        image = read_screenshot(path)
        self.path_var.set(str(path))
        self.load_image_array(image)
        return image

    def choose_image(self) -> None:
        from tkinter import filedialog

        path = filedialog.askopenfilename(
            parent=self.top,
            title="选择港口截图（船卡在底部轮播条可见）",
            filetypes=[("图片", "*.png *.jpg *.jpeg *.bmp"), ("全部文件", "*.*")],
        )
        if not path:
            return
        try:
            self.load_screenshot(path)
        except ValueError as exc:
            self._set_status(f"截图读取失败：{exc}")

    def select_box(self, box: cc.BoxSelection) -> None:
        """框选结果入口（测试可直接调用，等价于用户在画布上拖出矩形）。"""

        self._on_box_selected(box)

    def _on_box_selected(self, box: cc.BoxSelection) -> None:
        if self._screenshot is None:
            self._set_status("请先选择截图。")
            return
        try:
            card = crop_card(self._screenshot, box)
        except ValueError as exc:
            self._set_status(f"框选失败：{exc}")
            return
        quality = inspect_card(card)
        self._quality = quality
        self.quality_label.configure(
            text=f"质量检查：{'✔ 通过' if quality.ok else '✗ 不合格'}（{quality.summary()}）"
        )
        if not quality.ok:
            self._card_image = None
            self._can_save = False
            self._set_status("模板质量不合格：" + "；".join(quality.errors) + "。请重新框选。")
            return
        self._card_image = card
        self._set_status("模板质量通过；请填写船名后保存。")
        self._refresh_conflicts()

    # -- 名称与冲突 ---------------------------------------------------
    def set_ship_name(self, text: str) -> None:
        self.name_var.set(text)

    @property
    def ship_name(self) -> str:
        return self.name_var.get().strip()

    @property
    def ship_id(self) -> str | None:
        return self._target_ship_id

    @property
    def conflicts(self) -> tuple[ShipCandidate, ...]:
        return self._conflicts

    @property
    def status_text(self) -> str:
        return self._status

    @property
    def quality(self) -> cc.TemplateQuality | None:
        return self._quality

    @property
    def selected_card(self) -> np.ndarray | None:
        return self._card_image

    def _refresh_conflicts(self) -> None:
        if self._card_image is None:
            self._conflicts = ()
            self.conflict_frame.pack_forget()
            self._can_save = False
            self._refresh_status()
            return
        name = self.ship_name
        self._conflicts = display_name_conflicts(self.project_root, self.profile_id, name)
        self._override = None
        self._overwrite = False
        if not name:
            self.ship_id_var.set(suggest_ship_id(self.project_root, self.profile_id))
            self._target_ship_id = self.ship_id_var.get()
            self._can_save = True
            self.conflict_frame.pack_forget()
        elif self._conflicts:
            self._target_ship_id = self._conflicts[0].ship_id
            self.ship_id_var.set(self._target_ship_id)
            self._can_save = False
            self.conflict_label.configure(
                text=(
                    f"显示名「{name}」已存在：{self._conflicts[0].display_name}"
                    f"（{self._conflicts[0].ship_id}）。请选择 更新现有档案 / 另存新档案 / 取消，"
                    "不会静默覆盖。"
                )
            )
            if not self.conflict_frame.winfo_manager():
                self.conflict_frame.pack(fill="x", pady=(6, 0))
        else:
            target = suggest_ship_id(self.project_root, self.profile_id)
            self._target_ship_id = target
            self.ship_id_var.set(target)
            self._can_save = True
            self.conflict_frame.pack_forget()
        self._refresh_status()

    def resolve_update(self) -> None:
        """用户显式选择“更新现有档案”（保存时还会二次确认）。"""

        if not self._conflicts:
            self._set_status("没有可更新的冲突档案。")
            return
        self._override = "update"
        self._overwrite = True
        self._target_ship_id = self._conflicts[0].ship_id
        self.ship_id_var.set(self._target_ship_id)
        self.conflict_frame.pack_forget()
        self._can_save = True
        self._set_status(
            f"将更新现有档案：{self._conflicts[0].display_name}（{self._conflicts[0].ship_id}）；"
            "点击“保存并使用”时会再次确认。"
        )

    def resolve_save_new(self) -> None:
        """用户选择“另存新档案”：生成新 ship_id，显示名保持用户输入。"""

        self._override = "save_new"
        self._overwrite = False
        target = suggest_ship_id(self.project_root, self.profile_id)
        self._target_ship_id = target
        self.ship_id_var.set(target)
        self.conflict_frame.pack_forget()
        self._can_save = True
        self._set_status(f"将另存为新档案：{self.ship_name}（{target}）。")

    def cancel_conflict(self) -> None:
        self._override = None
        self._overwrite = False
        self._can_save = False
        self.conflict_frame.pack_forget()
        self._set_status("已取消本次冲突处理：未写入任何文件。")

    # -- 保存 ---------------------------------------------------------
    def save(self) -> ShipSaveResult | None:
        if self._card_image is None:
            self._set_status("请先框选合格的船卡模板。")
            return None
        if not self.ship_name:
            self._set_status("请填写船名（只输入名称，不做 OCR）。")
            return None
        if self.conflicts and self._override is None:
            self._set_status("存在显示名冲突：请先选择 更新现有档案 / 另存新档案 / 取消（不静默覆盖）。")
            return None
        if self._target_ship_id is None:
            if self.conflicts:
                self._target_ship_id = self.conflicts[0].ship_id
            else:
                self._target_ship_id = suggest_ship_id(self.project_root, self.profile_id)
        if self._override == "update":
            conflict = self.conflicts[0] if self.conflicts else None
            if conflict is not None and not self._confirm_update(conflict):
                self._set_status("已取消更新现有档案：未写入任何文件。")
                return None
        try:
            result = save_ship_card(
                self.project_root,
                self.profile_id,
                self._target_ship_id,
                self.ship_name,
                self._card_image,
                overwrite=self._overwrite,
            )
        except ShipImportError as exc:
            self._set_status(str(exc))
            return None
        self._can_save = False
        self._set_status(
            f"已保存：{result.display_name}（{result.ship_id}）→ {result.directory}；"
            "尚未实机验证识别效果，请重新校验配置后再启动。"
        )
        if self._on_saved is not None:
            self._on_saved(result)
        if self._close_on_save:
            self.close()
        return result

    def _ask_update(self, conflict: ShipCandidate) -> bool:
        from tkinter import messagebox

        return bool(
            messagebox.askyesno(
                "确认更新现有档案",
                f"将覆盖现有船卡与 ship.yaml：{conflict.display_name}（{conflict.ship_id}）。\n"
                "此操作不可撤销，是否继续？",
                parent=self.top,
            )
        )

    def _set_status(self, text: str) -> None:
        self._status = text
        self._refresh_status()

    def _refresh_status(self) -> None:
        try:
            self.status_label.configure(text=self._status)
            self.save_button.configure(state="normal" if self._can_save else "disabled")
        except Exception:  # noqa: BLE001 - 窗口已销毁时忽略刷新
            pass

    def close(self) -> None:
        try:
            self.canvas.frame.destroy()
            self.top.destroy()
        except Exception:  # noqa: BLE001 - 重复关闭/父窗口先销毁
            pass


__all__ = [
    "DEFAULT_CARD_SEARCH_REGION",
    "DEFAULT_NOTES",
    "SHIP_YAML_HEADER",
    "ShipImportDialog",
    "ShipImportError",
    "ShipSaveResult",
    "build_ship_yaml",
    "crop_card",
    "display_name_conflicts",
    "inspect_card",
    "list_ship_candidates",
    "read_screenshot",
    "save_ship_card",
    "ship_directory",
    "ships_root",
    "suggest_ship_id",
    "validate_ship_identity",
]
