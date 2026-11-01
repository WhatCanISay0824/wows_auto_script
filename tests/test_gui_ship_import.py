"""船卡导入测试（M2）：合成图片、质量拒绝、冲突不静默覆盖、保存后列表可见。

纯函数测试不需要 Tk；对话框测试在 Tk 不可用时 ``pytest.skip``。
所有写入都发生在 ``tmp_path``，不产生仓库运行产物、不复制原始港口截图。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from src.gui.ship_import import (
    DEFAULT_CARD_SEARCH_REGION,
    ShipImportDialog,
    ShipImportError,
    ShipSaveResult,
    build_ship_yaml,
    display_name_conflicts,
    inspect_card,
    list_ship_candidates,
    save_ship_card,
    suggest_ship_id,
    validate_ship_identity,
)
from src.tools import calibrate_common as cc


def make_card_image(width: int = 80, height: int = 140) -> np.ndarray:
    """合成一张有颜色纹理的 BGR 图片（灰度标准差足够通过质量检查）。"""

    xs = np.linspace(0, 255, width, dtype=np.uint8)
    ys = np.linspace(0, 255, height, dtype=np.uint8)
    gray = ((xs[None, :].astype(np.int32) + ys[:, None].astype(np.int32)) // 2).astype(np.uint8)
    blue = np.roll(gray, 5, axis=1)
    red = np.roll(gray, 9, axis=0)
    return np.dstack([blue, gray, red]).astype(np.uint8)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "profiles" / "cn360" / "ships").mkdir(parents=True)
    (root / "config.yaml").write_text("runtime:\n  loop_hz: 10\n", encoding="utf-8")
    return root


def test_quality_check_passes_for_textured_card() -> None:
    quality = inspect_card(make_card_image())
    assert quality.ok, quality.errors
    assert quality.width == 80 and quality.height == 140


def test_save_new_ship_creates_card_and_yaml(project: Path) -> None:
    image = make_card_image()
    result = save_ship_card(project, "cn360", "ship_001", "新船名称", image)
    assert isinstance(result, ShipSaveResult)
    assert result.updated is False
    assert result.card_path.is_file() and result.yaml_path.is_file()
    assert result.directory == project / "profiles" / "cn360" / "ships" / "ship_001"
    data = cc.read_yaml_mapping(result.yaml_path)
    assert data["ship_id"] == "ship_001"
    assert data["display_name"] == "新船名称"
    assert data["card_template"] == "card.png"
    assert tuple(data["card_search_region"]) == DEFAULT_CARD_SEARCH_REGION
    assert data["loadout_slots"] == []  # 附录 D Q3：允许空槽
    assert "未实机验证" in str(data["notes"])

    candidates = list_ship_candidates(project, "cn360")
    assert [(item.ship_id, item.display_name) for item in candidates] == [("ship_001", "新船名称")]
    assert suggest_ship_id(project, "cn360") == "ship_002"


def test_quality_rejects_too_small_and_flat(project: Path) -> None:
    tiny = np.zeros((4, 4, 3), dtype=np.uint8)
    with pytest.raises(ShipImportError) as exc_info:
        save_ship_card(project, "cn360", "ship_001", "小图", tiny)
    assert "小于" in str(exc_info.value)
    assert not (project / "profiles" / "cn360" / "ships" / "ship_001").exists()

    flat = np.full((40, 40, 3), 128, dtype=np.uint8)
    with pytest.raises(ShipImportError) as exc_info:
        save_ship_card(project, "cn360", "ship_001", "纯色", flat)
    assert "纯色" in str(exc_info.value)
    assert not (project / "profiles" / "cn360" / "ships" / "ship_001").exists()


def test_existing_ship_not_overwritten_without_explicit_confirm(project: Path) -> None:
    first = save_ship_card(project, "cn360", "ship_001", "铁公爵", make_card_image(80, 140))
    original_card = first.card_path.read_bytes()
    original_yaml = first.yaml_path.read_text(encoding="utf-8")
    other_image = make_card_image(120, 160)
    with pytest.raises(ShipImportError) as exc_info:
        save_ship_card(project, "cn360", "ship_001", "铁公爵", other_image)
    assert "不静默覆盖" in str(exc_info.value)
    assert first.card_path.read_bytes() == original_card
    assert first.yaml_path.read_text(encoding="utf-8") == original_yaml

    updated = save_ship_card(project, "cn360", "ship_001", "铁公爵", other_image, overwrite=True)
    assert updated.updated is True
    assert updated.card_path.read_bytes() != original_card


def test_display_name_conflict_detection(project: Path) -> None:
    save_ship_card(project, "cn360", "iron_duke", "铁公爵", make_card_image())
    conflicts = display_name_conflicts(project, "cn360", "铁公爵")
    assert [item.ship_id for item in conflicts] == ["iron_duke"]
    assert display_name_conflicts(project, "cn360", "铁公爵", exclude_ship_id="iron_duke") == ()
    assert display_name_conflicts(project, "cn360", "") == ()
    # “另存新档案”：同名不同船号允许（由界面显式选择后调用），不覆盖旧档案
    save_ship_card(project, "cn360", "ship_002", "铁公爵", make_card_image(90, 150))
    assert len(list_ship_candidates(project, "cn360")) == 2
    assert len(display_name_conflicts(project, "cn360", "铁公爵")) == 2


def test_suggest_ship_id_skips_used_numbers(project: Path) -> None:
    save_ship_card(project, "cn360", "ship_001", "A", make_card_image())
    save_ship_card(project, "cn360", "ship_003", "B", make_card_image())
    assert suggest_ship_id(project, "cn360") == "ship_002"
    (project / "profiles" / "cn360" / "ships" / "ship_002").mkdir()
    assert suggest_ship_id(project, "cn360") == "ship_004"


def test_identity_validation() -> None:
    assert validate_ship_identity("ship_001", "新船") == ()
    assert validate_ship_identity("非法 ID", "新船")
    assert validate_ship_identity("ship_001", "   ")
    with pytest.raises(ShipImportError):
        save_ship_card(Path("."), "cn360", "BAD ID", "名字", make_card_image())


def test_build_ship_yaml_minimal_fields() -> None:
    payload = build_ship_yaml("ship_009", "  测试船  ")
    assert payload["ship_id"] == "ship_009"
    assert payload["display_name"] == "测试船"
    assert payload["loadout_slots"] == []
    assert payload["scroll_direction"] == "down"
    assert "ship_id" in payload and "notes" in payload


# ---------------------------------------------------------------------------
# Tk 对话框（Tk 不可用时 skip）
# ---------------------------------------------------------------------------


@pytest.fixture
def tk_root():
    tk = pytest.importorskip("tkinter")
    import time

    last_error: Exception | None = None
    for _attempt in range(3):
        try:
            root = tk.Tk()
        except tk.TclError as exc:  # 偶发的 Tcl 库读取竞争：短暂重试后仍失败才跳过
            last_error = exc
            time.sleep(0.2)
            continue
        root.withdraw()
        try:
            yield root
        finally:
            try:
                root.destroy()
            except tk.TclError:
                pass
        return
    pytest.skip(f"Tk 不可用或没有桌面会话：{last_error}")


def _make_dialog(tk_root, project: Path, saved: list[ShipSaveResult]) -> ShipImportDialog:
    return ShipImportDialog(
        tk_root,
        project_root=project,
        profile_id="cn360",
        on_saved=saved.append,
        confirm_update=lambda _conflict: True,
        close_on_save=False,
    )


def test_dialog_save_flow(tk_root, project: Path) -> None:
    saved: list[ShipSaveResult] = []
    dialog = _make_dialog(tk_root, project, saved)
    try:
        image = make_card_image()
        dialog.load_image_array(image)
        dialog.select_box(cc.BoxSelection(0, 0, image.shape[1], image.shape[0]))
        assert dialog.quality is not None and dialog.quality.ok
        dialog.set_ship_name("新船")
        result = dialog.save()
        assert result is not None and result.ship_id == "ship_001"
        assert saved and saved[0].ship_id == "ship_001"
        assert "尚未实机验证" in dialog.status_text
        assert list_ship_candidates(project, "cn360")[0].display_name == "新船"
    finally:
        dialog.close()


def test_dialog_quality_rejection_blocks_save(tk_root, project: Path) -> None:
    saved: list[ShipSaveResult] = []
    dialog = _make_dialog(tk_root, project, saved)
    try:
        dialog.load_image_array(make_card_image())
        dialog.select_box(cc.BoxSelection(0, 0, 4, 4))
        assert dialog.quality is not None and not dialog.quality.ok
        assert "不合格" in dialog.status_text
        dialog.set_ship_name("小图")
        assert dialog.save() is None
        assert not (project / "profiles" / "cn360" / "ships" / "ship_001").exists()
    finally:
        dialog.close()


def test_dialog_conflict_three_way_flow(tk_root, project: Path) -> None:
    save_ship_card(project, "cn360", "iron_duke", "铁公爵", make_card_image())
    saved: list[ShipSaveResult] = []
    dialog = _make_dialog(tk_root, project, saved)
    try:
        dialog.load_image_array(make_card_image())
        dialog.select_box(cc.BoxSelection(0, 0, 80, 140))
        dialog.set_ship_name("铁公爵")
        assert [item.ship_id for item in dialog.conflicts] == ["iron_duke"]
        # 未选择处理方式时不允许保存（不静默覆盖）
        assert dialog.save() is None
        assert "冲突" in dialog.status_text
        # 另存新档案：生成新船号，旧档案保留
        dialog.resolve_save_new()
        assert dialog.ship_id is not None and dialog.ship_id != "iron_duke"
        result = dialog.save()
        assert result is not None and result.ship_id == dialog.ship_id
        assert len(list_ship_candidates(project, "cn360")) == 2
        assert (project / "profiles" / "cn360" / "ships" / "iron_duke" / "card.png").is_file()
    finally:
        dialog.close()


def test_dialog_update_requires_confirm(tk_root, project: Path) -> None:
    original = save_ship_card(project, "cn360", "iron_duke", "铁公爵", make_card_image())
    original_card = original.card_path.read_bytes()
    saved: list[ShipSaveResult] = []
    dialog = ShipImportDialog(
        tk_root,
        project_root=project,
        profile_id="cn360",
        on_saved=saved.append,
        confirm_update=lambda _conflict: False,  # 用户拒绝二次确认
        close_on_save=False,
    )
    try:
        dialog.load_image_array(make_card_image(120, 160))
        dialog.select_box(cc.BoxSelection(0, 0, 120, 160))
        dialog.set_ship_name("铁公爵")
        dialog.resolve_update()
        assert dialog.save() is None
        assert "取消更新" in dialog.status_text
        assert original_card == (project / "profiles" / "cn360" / "ships" / "iron_duke" / "card.png").read_bytes()
        assert saved == []
    finally:
        dialog.close()
