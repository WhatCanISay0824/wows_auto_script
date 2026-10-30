"""测试用配置工程构造器（T1 配置/校验/地图测试共用）。

在 ``tmp_path`` 下生成一个“已完成标定”的完整工程：
- ``config.yaml``（只写必要覆盖，其余用模型默认值）；
- ``profiles/testclient/profile.yaml`` + ``keys.yaml`` + 全部特征模板 PNG；
- ``profiles/testclient/ships/test_ship/ship.yaml`` + 卡片与空装备槽模板 PNG；
- ``maps/test_map/`` 的 ``map.yaml`` / ``spawns.yaml`` / ``routes.yaml`` / ``background.png``。

模板图片用合成噪声图（>=16×16、非纯色），满足校验器对尺寸/纯色的要求；
正式运行所需的实机模板仍由标定工具生成，不能拿这些图代替。
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import yaml
from tests.helpers import write_yaml

# 客户端/船只/地图编号（测试专用）
CLIENT_ID = "testclient"
SHIP_ID = "test_ship"
MAP_ID = "test_map"
# 参考分辨率
REFERENCE_RESOLUTION = (2560, 1600)
# 静态账号档案 fixture（T7 测试用）
ACCOUNTS_FIXTURE = Path(__file__).resolve().parents[1] / "accounts.yaml"

# 与 profiles/cn360/profile.yaml 同构的特征表（feature_id -> (template, region, threshold)）
FEATURE_SPECS: dict[str, tuple[str, list[float], float]] = {
    "port_indicator": ("features/port_indicator.png", [0.00, 0.00, 0.35, 0.12], 0.85),
    "port_battle_button": ("features/port_battle_button.png", [0.55, 0.82, 0.98, 0.99], 0.85),
    "port_mode_selector": ("features/port_mode_selector.png", [0.55, 0.70, 0.98, 0.85], 0.85),
    "coop_mode_selected": ("features/coop_mode_selected.png", [0.55, 0.70, 0.98, 0.85], 0.85),
    "port_ship_panel_button": ("features/port_ship_panel_button.png", [0.00, 0.78, 0.22, 0.99], 0.85),
    "queue_indicator": ("features/queue_indicator.png", [0.30, 0.35, 0.70, 0.75], 0.85),
    "cancel_queue_button": ("features/cancel_queue_button.png", [0.30, 0.70, 0.70, 0.90], 0.85),
    "loading_indicator": ("features/loading_indicator.png", [0.30, 0.40, 0.70, 0.70], 0.85),
    "loading_tip_area": ("features/loading_tip_area.png", [0.10, 0.78, 0.90, 0.98], 0.85),
    "battle_hud_indicator": ("features/battle_hud_indicator.png", [0.00, 0.85, 0.25, 0.99], 0.85),
    "hp_bar_present": ("features/hp_bar_present.png", [0.02, 0.86, 0.20, 0.93], 0.85),
    "minimap_frame": ("features/minimap_frame.png", [0.84, 0.01, 0.995, 0.26], 0.85),
    "death_banner": ("features/death_banner.png", [0.25, 0.30, 0.75, 0.70], 0.80),
    "spectator_indicator": ("features/spectator_indicator.png", [0.45, 0.00, 0.99, 0.16], 0.85),
    "battle_results": ("features/battle_results.png", [0.25, 0.10, 0.75, 0.45], 0.85),
    "result_panel": ("features/result_panel.png", [0.10, 0.10, 0.90, 0.90], 0.85),
    "result_return_port_button": ("features/result_return_port_button.png", [0.60, 0.82, 0.99, 0.99], 0.85),
    "exit_menu_panel": ("features/exit_menu_panel.png", [0.25, 0.15, 0.75, 0.85], 0.85),
    "exit_menu_return_port": ("features/exit_menu_return_port.png", [0.25, 0.55, 0.75, 0.90], 0.85),
    "exit_confirm_dialog": ("features/exit_confirm_dialog.png", [0.30, 0.35, 0.70, 0.65], 0.85),
    "exit_confirm_yes": ("features/exit_confirm_yes.png", [0.50, 0.55, 0.80, 0.70], 0.85),
    "popup_close_button": ("features/popup_close_button.png", [0.85, 0.05, 0.99, 0.20], 0.85),
    "reward_panel": ("features/reward_panel.png", [0.15, 0.10, 0.85, 0.90], 0.85),
    "mission_panel": ("features/mission_panel.png", [0.15, 0.10, 0.85, 0.90], 0.85),
}


@dataclass(frozen=True, slots=True)
class TestProject:
    """一个已生成测试工程的路径信息。"""

    root: Path
    client_id: str = CLIENT_ID
    ship_id: str = SHIP_ID
    map_id: str = MAP_ID

    @property
    def profile_dir(self) -> Path:
        return self.root / "profiles" / self.client_id

    @property
    def profile_path(self) -> Path:
        return self.profile_dir / "profile.yaml"

    @property
    def keys_path(self) -> Path:
        return self.profile_dir / "keys.yaml"

    @property
    def ship_dir(self) -> Path:
        return self.profile_dir / "ships" / self.ship_id

    @property
    def ship_path(self) -> Path:
        return self.ship_dir / "ship.yaml"

    @property
    def maps_dir(self) -> Path:
        return self.root / "maps"

    @property
    def map_dir(self) -> Path:
        return self.maps_dir / self.map_id

    @property
    def map_path(self) -> Path:
        return self.map_dir / "map.yaml"

    @property
    def spawns_path(self) -> Path:
        return self.map_dir / "spawns.yaml"

    @property
    def routes_path(self) -> Path:
        return self.map_dir / "routes.yaml"

    @property
    def accounts_path(self) -> Path:
        return self.root / "accounts.yaml"


# ---------------------------------------------------------------------------
# 合成图片
# ---------------------------------------------------------------------------


def template_image(seed: int = 0, size: int = 16) -> np.ndarray:
    """生成非纯色的合成模板图（带角点/线条，适合做文件校验与模板匹配样本）。"""
    rng = np.random.default_rng(seed)
    image = np.full((size, size, 3), 40, dtype=np.uint8)
    for _ in range(max(3, size // 4)):
        point = tuple(int(value) for value in rng.integers(0, size, 2))
        color = tuple(int(value) for value in rng.integers(60, 255, 3))
        cv2.circle(image, point, int(max(1, size // 8)), color, thickness=-1)
    for _ in range(2):
        start = tuple(int(value) for value in rng.integers(0, size, 2))
        end = tuple(int(value) for value in rng.integers(0, size, 2))
        color = tuple(int(value) for value in rng.integers(80, 255, 3))
        cv2.line(image, start, end, color, thickness=1)
    return image


def write_png(path: Path, image: np.ndarray) -> Path:
    """写 PNG（用 imencode + tofile，兼容非 ASCII 路径）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    success, buffer = cv2.imencode(".png", image)
    if not success:  # pragma: no cover - 合成图编码不会失败
        raise RuntimeError("PNG 编码失败")
    buffer.tofile(str(path))
    return path


def uniform_image(size: int = 16, color: tuple[int, int, int] = (10, 10, 10)) -> np.ndarray:
    """纯色图（用于校验“纯色模板被拒绝”）。"""
    return np.full((size, size, 3), color, dtype=np.uint8)


# ---------------------------------------------------------------------------
# YAML 片段
# ---------------------------------------------------------------------------


def global_config_data(ship_id: str = SHIP_ID) -> dict[str, Any]:
    """最小 config.yaml：只覆盖测试关注字段，其余取模型默认值。"""
    return {
        "runtime": {"loop_hz": 10, "capture_hz": 2, "debug": False, "log_level": "INFO"},
        "ship": {"ship_id": ship_id, "verify_each_battle": True},
        "map": {"auto_identify": True},
        "accounts": {
            "rotation_enabled": False,
            "rotation_mode": "round_robin",
            "max_battles_per_account": 3,
            "accounts_file": "accounts.yaml",
        },
    }


def profile_data(client_id: str = CLIENT_ID) -> dict[str, Any]:
    """与 profiles/cn360/profile.yaml 同构、模板齐全的客户端配置。"""
    features: dict[str, Any] = {}
    for feature_id, (template, region, threshold) in FEATURE_SPECS.items():
        features[feature_id] = {"template": template, "region": region, "threshold": threshold}
    return {
        "client_id": client_id,
        "display_name": "测试客户端",
        "language": "zh_cn",
        "reference_resolution": list(REFERENCE_RESOLUTION),
        "notes": "自动化测试配置",
        "window": {
            "process_names": ["WorldOfWarships.exe"],
            "title_patterns": ["战舰世界"],
            "require_foreground": True,
        },
        "keys_file": "keys.yaml",
        "minimap": {
            "rect": [0.86, 0.03, 0.99, 0.24],
            "border_px": 2,
            "frame_feature": "minimap_frame",
            "marker_template": "marker/marker.png",
            "marker_angles": 24,
            "marker_hsv_lower": [0, 0, 200],
            "marker_hsv_upper": [180, 60, 255],
            "marker_jump_px": 300,
            "marker_lost_s": 3,
        },
        "battle_hud": {
            "hp_bar_region": [0.02, 0.86, 0.20, 0.93],
            "hp_fill_hsv_lower": [35, 80, 80],
            "hp_fill_hsv_upper": [95, 255, 255],
            "hp_fill_zero_ratio": 0.03,
            "spectator_features": ["spectator_indicator"],
            "death_banner_feature": "death_banner",
            "result_feature": "battle_results",
        },
        "camera": {"relative": True, "sensitivity": 1.0},
        "exit": {
            "menu_key": "escape",
            "menu_open_feature": "exit_menu_panel",
            "return_port_feature": "exit_menu_return_port",
            "confirm_feature": "exit_confirm_yes",
            "port_feature": "port_indicator",
            "direct_exit_feature": None,
            "post_click_wait_ms": 500,
        },
        "state_features": {
            "PORT": ["port_indicator", "port_battle_button", "port_mode_selector", "coop_mode_selected"],
            "QUEUEING": ["queue_indicator", "cancel_queue_button"],
            "LOADING": ["loading_indicator", "loading_tip_area"],
            "BATTLE_ACTIVE": ["battle_hud_indicator", "hp_bar_present", "minimap_frame"],
            "BATTLE_FINISHED": ["battle_results", "result_panel"],
            "EXIT_MENU_OPEN": ["exit_menu_panel", "exit_menu_return_port"],
            "EXIT_CONFIRMING": ["exit_confirm_dialog", "exit_confirm_yes"],
            "POST_EXIT_INTERFACE": ["popup_close_button", "reward_panel", "mission_panel"],
        },
        "popup_features": ["reward_panel", "mission_panel"],
        "features": features,
        "start_sequence": [
            {"action": "wait_feature", "feature": "coop_mode_selected", "timeout_ms": 2000, "retries": 0},
            {"action": "click_feature", "feature": "port_battle_button", "timeout_ms": 3000, "retries": 1},
            {"action": "wait_feature", "feature": "queue_indicator", "timeout_ms": 15000, "retries": 0},
        ],
        "start_sequence_full": [
            {"action": "click_feature", "feature": "port_mode_selector", "timeout_ms": 3000, "retries": 1},
            {"action": "click_feature", "feature": "coop_mode_selected", "timeout_ms": 5000, "retries": 1},
            {"action": "click_feature", "feature": "port_battle_button", "timeout_ms": 3000, "retries": 1},
            {"action": "wait_feature", "feature": "queue_indicator", "timeout_ms": 15000, "retries": 0},
        ],
        "result_sequence": [
            {"action": "wait_feature", "feature": "battle_results", "timeout_ms": 60000, "retries": 0},
            {
                "action": "click_feature",
                "feature": "result_return_port_button",
                "timeout_ms": 10000,
                "retries": 2,
                "offset_x": 4,
                "offset_y": -4,
            },
        ],
        "popup_sequence": [
            {"action": "click_feature", "feature": "popup_close_button", "timeout_ms": 3000, "retries": 1}
        ],
        "ship_selection_sequence": [
            {"action": "click_feature", "feature": "port_ship_panel_button", "timeout_ms": 3000, "retries": 1}
        ],
        "ship_verify_features": ["ship_card"],
        "loadout_verify_features": ["loadout_signal_flags", "loadout_economic_bonus"],
        "clear_loadout_sequence": None,
    }


def keys_data() -> dict[str, Any]:
    return {
        "actions": {"escape": "escape", "fire": "mouse_left", "full_map": "m"},
        "camera": {"relative": True, "sensitivity": 1.0},
        "consumables": [
            {"key": "1", "name": "损管小组"},
            {"key": "2", "name": "维修小组"},
        ],
    }


def ship_data(ship_id: str = SHIP_ID) -> dict[str, Any]:
    return {
        "ship_id": ship_id,
        "display_name": "测试船只",
        "card_template": "card.png",
        "card_search_region": None,
        "loadout_preset_feature": None,
        "max_scrolls": 20,
        "scroll_direction": "down",
        "scroll_clicks": 1,
        "loadout_slots": [
            {"slot_id": "signal_flags", "name": "信号旗槽位", "empty_template": "loadout/signal_flags_empty.png"},
            {
                "slot_id": "economic_bonus",
                "name": "经济加成槽位",
                "empty_template": "loadout/economic_bonus_empty.png",
            },
        ],
        "notes": "测试船只",
    }


def map_data(map_id: str = MAP_ID) -> dict[str, Any]:
    return {
        "map_id": map_id,
        "name": "测试地图",
        "size_meters": [12000, 12000],
        "world_bounds": [-6000, -6000, 6000, 6000],
        "image_bounds": [20, 20, 780, 780],
        "image_orientation": "normal",
        "world_to_image": None,
        "default_minimap_rect": None,
        "notes": "测试地图",
    }


def spawns_data() -> dict[str, Any]:
    return {
        "spawns": [
            {"spawn_id": "east_1", "position": [4000, -4000], "facing_deg": 270, "label": "东侧一号位"},
            {"spawn_id": "west_1", "position": [-4000, 4000], "facing_deg": 90, "label": "西侧一号位"},
        ]
    }


def routes_data() -> dict[str, Any]:
    return {
        "routes": [
            {
                "route_id": "east_1_south",
                "spawn_id": "east_1",
                "weight": 1.0,
                "description": "向南航行",
                "waypoints": [[3500, -3500], [1500, -1500], [0, 1000]],
            },
            {
                "route_id": "west_1_center",
                "spawn_id": "west_1",
                "weight": 0.6,
                "description": "向中心航行",
                "waypoints": [[-3500, 3500], [-1500, 1500], [0, 0]],
            },
        ]
    }


# ---------------------------------------------------------------------------
# 工程构造
# ---------------------------------------------------------------------------


def build_valid_project(root: Path, *, background_size: int = 800, with_accounts: bool = True) -> TestProject:
    """在 root 下生成一个校验可通过的完整测试工程（含可选账号档案文件）。"""
    project = TestProject(root=root)
    # 全局配置
    write_yaml(root / "config.yaml", global_config_data())
    # 账号档案（可选；无账号档案时应只有 warning，不阻止运行）
    if with_accounts:
        shutil.copyfile(ACCOUNTS_FIXTURE, project.accounts_path)
    # 客户端配置与模板
    write_yaml(project.profile_path, profile_data())
    write_yaml(project.keys_path, keys_data())
    for seed, (_, spec) in enumerate(FEATURE_SPECS.items()):
        write_png(project.profile_dir / spec[0], template_image(seed=seed))
    write_png(project.profile_dir / "marker" / "marker.png", template_image(seed=101))
    # 船只
    write_yaml(project.ship_path, ship_data())
    write_png(project.ship_dir / "card.png", template_image(seed=201))
    for slot_seed, name in enumerate(("signal_flags_empty.png", "economic_bonus_empty.png")):
        write_png(project.ship_dir / "loadout" / name, template_image(seed=300 + slot_seed))
    # 地图包
    write_yaml(project.map_path, map_data())
    write_yaml(project.spawns_path, spawns_data())
    write_yaml(project.routes_path, routes_data())
    from tests.helpers import sample_background

    write_png(project.map_dir / "background.png", sample_background(width=background_size, height=background_size))
    return project


# ---------------------------------------------------------------------------
# 修改辅助（测试构造非法配置）
# ---------------------------------------------------------------------------


def load_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def update_yaml(path: Path, updater: Callable[[dict[str, Any]], None]) -> None:
    """读取 YAML -> 修改 -> 写回（测试构造非法/边界配置）。"""
    data = load_yaml(path)
    if not isinstance(data, dict):
        raise TypeError(f"期望映射: {path}")
    updater(data)
    write_yaml(path, data)


def set_mapping(data: dict[str, Any], dotted: str, value: Any) -> None:
    """按点分路径设置嵌套字典的值。"""
    parts = dotted.split(".")
    target = data
    for part in parts[:-1]:
        nested = target.get(part)
        if not isinstance(nested, dict):
            nested = {}
            target[part] = nested
        target = nested
    target[parts[-1]] = value


def delete_mapping(data: dict[str, Any], dotted: str) -> None:
    """按点分路径删除嵌套字典的键。"""
    parts = dotted.split(".")
    target: Any = data
    for part in parts[:-1]:
        target = target.get(part)
        if not isinstance(target, dict):
            return
    if isinstance(target, dict):
        target.pop(parts[-1], None)


def edit_yaml_file(path: Path, dotted: str, value: Any) -> None:
    """修改 YAML 文件中某个点分路径的值。"""
    update_yaml(path, lambda data: set_mapping(data, dotted, value))


def delete_yaml_key(path: Path, dotted: str) -> None:
    """删除 YAML 文件中某个点分路径的键。"""
    update_yaml(path, lambda data: delete_mapping(data, dotted))


def rewrite_map_bundle(project: TestProject, map_data_override: Mapping[str, Any] | None = None, *, spacing: float = 2.0) -> None:
    """按给定覆盖重写 map.yaml/spawns.yaml/routes.yaml（保持背景图不变）。"""
    data = map_data()
    if map_data_override:
        data.update(dict(map_data_override))
    write_yaml(project.map_path, data)
    write_yaml(project.spawns_path, spawns_data())
    routes = routes_data()
    if spacing != 1.0:
        for route in routes["routes"]:
            route["waypoints"] = [[x * spacing, y * spacing] for x, y in route["waypoints"]]
    write_yaml(project.routes_path, routes)
