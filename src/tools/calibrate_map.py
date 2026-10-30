"""地图标定 GUI（详细设计 12.1 / 12.2）。

用法：::

    python -m src.tools.calibrate_map --map-dir maps/north_sea
    python -m src.tools.calibrate_map --root . --map-id north_sea

界面流程（详细设计 12.2）：

1. 载入 ``maps/<map_id>/background.png`` 与已有 map.yaml/spawns.yaml/routes.yaml；
2. 拖动矩形标出可游玩区域 ``image_bounds``，填写 ``world_bounds`` / ``size_meters``，
   选择 ``image_orientation`` 并用「方向预览」确认底图方向；
3. 在底图上单击添加出生点（世界坐标），选择出生点后依次单击添加航点；
4. 保存前自动执行 ``validate.validate_map_pack`` 的全部 4.7 规则：
   有 error 阻止保存，有 warning 提示后再保存；
5. 保存 map.yaml / spawns.yaml / routes.yaml（保留文件开头的注释模板）。

GUI 与业务逻辑分离：加载、字段解析、校验、保存都是纯函数
（``load_editor_state`` / ``apply_field_texts`` / ``validate_editor_state`` /
``save_editor_state``），单元测试只验证这些函数，不创建窗口、不调用 mainloop。
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..common.coords import ImageOrientation
from ..common.errors import ValidationIssue
from ..common.types import NormalizedRect
from ..config.models import MapPack, Route, SpawnPoint
from ..config.validate import validate_map_pack
from . import calibrate_common as cc

#: 新建文件的注释模板（已有文件的注释块会被自动保留）
MAP_YAML_HEADER = """# 地图元数据（详细设计 4.2）
# 由 src/tools/calibrate_map 生成/更新；字段含义见设计文档 4.2。
# image_bounds 为可游玩区域在背景图中的像素范围，world_bounds 与 size_meters 单位为米。"""
SPAWNS_YAML_HEADER = """# 出生点列表（详细设计 4.3）
# 位置使用世界坐标（米），朝向以正北为 0 度、顺时针增加。"""
ROUTES_YAML_HEADER = """# 航线列表（详细设计 4.4）
# 航点为世界坐标（米）；首航点距出生点 <= 1500 米，相邻航点 200~10000 米。"""
#: map.yaml 中本工具接管的字段（其余未知字段原样保留）
KNOWN_MAP_FIELDS: frozenset[str] = frozenset(
    {
        "map_id",
        "name",
        "size_meters",
        "world_bounds",
        "image_bounds",
        "image_orientation",
        "world_to_image",
        "default_minimap_rect",
        "notes",
    }
)
#: 默认地图尺寸（米）
DEFAULT_MAP_SIZE = 12000.0


# ---------------------------------------------------------------------------
# 编辑数据模型
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SpawnEdit:
    """一个出生点（可编辑）。"""

    spawn_id: str
    x: float = 0.0
    y: float = 0.0
    facing_deg: float = 0.0
    label: str = ""

    @property
    def position(self) -> tuple[float, float]:
        return self.x, self.y

    def to_yaml(self) -> dict[str, Any]:
        return {
            "spawn_id": self.spawn_id,
            "position": [_round(self.x), _round(self.y)],
            "facing_deg": _round(self.facing_deg),
            "label": self.label,
        }

    @classmethod
    def from_yaml(cls, data: Any) -> SpawnEdit | None:
        if not isinstance(data, dict):
            return None
        spawn_id = str(data.get("spawn_id") or "").strip()
        if not spawn_id:
            return None
        position = _float_list(data.get("position"), 2)
        x, y = position if position is not None else (0.0, 0.0)
        facing = _float_or(data.get("facing_deg"), 0.0)
        return cls(spawn_id=spawn_id, x=x, y=y, facing_deg=facing, label=str(data.get("label") or ""))


@dataclass(slots=True)
class RouteEdit:
    """一条航线（可编辑）。"""

    route_id: str
    spawn_id: str
    weight: float = 1.0
    description: str = ""
    waypoints: list[tuple[float, float]] = field(default_factory=list)

    def to_yaml(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "spawn_id": self.spawn_id,
            "weight": _round(self.weight),
            "description": self.description,
            "waypoints": [[_round(x), _round(y)] for x, y in self.waypoints],
        }

    @classmethod
    def from_yaml(cls, data: Any) -> RouteEdit | None:
        if not isinstance(data, dict):
            return None
        route_id = str(data.get("route_id") or "").strip()
        if not route_id:
            return None
        waypoints: list[tuple[float, float]] = []
        raw_waypoints = data.get("waypoints")
        if isinstance(raw_waypoints, (list, tuple)):
            for item in raw_waypoints:
                pair = _float_list(item, 2)
                if pair is not None:
                    waypoints.append((pair[0], pair[1]))
        return cls(
            route_id=route_id,
            spawn_id=str(data.get("spawn_id") or ""),
            weight=_float_or(data.get("weight"), 1.0),
            description=str(data.get("description") or ""),
            waypoints=waypoints,
        )


@dataclass(slots=True)
class MapEditorState:
    """地图标定界面的全部可编辑状态。"""

    map_dir: Path
    map_id: str
    name: str = ""
    size_meters: tuple[float, float] = (DEFAULT_MAP_SIZE, DEFAULT_MAP_SIZE)
    world_bounds: tuple[float, float, float, float] = (
        -DEFAULT_MAP_SIZE / 2,
        -DEFAULT_MAP_SIZE / 2,
        DEFAULT_MAP_SIZE / 2,
        DEFAULT_MAP_SIZE / 2,
    )
    image_bounds: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)
    orientation: ImageOrientation = ImageOrientation.NORMAL
    world_to_image: list[float] | None = None
    default_minimap_rect: list[float] | None = None
    notes: str = ""
    extra_map_fields: dict[str, Any] = field(default_factory=dict)
    background: np.ndarray | None = None
    background_path: Path | None = None
    spawns: list[SpawnEdit] = field(default_factory=list)
    routes: list[RouteEdit] = field(default_factory=list)
    load_messages: list[str] = field(default_factory=list)

    # --- 几何 ---------------------------------------------------------
    @property
    def image_size(self) -> tuple[int, int]:
        if self.background is None:
            return (0, 0)
        height, width = self.background.shape[:2]
        return int(width), int(height)

    def transform(self) -> Any:
        return cc.build_world_transform(self.world_bounds, self.image_bounds, self.orientation)

    def world_to_image_xy(self, x: float, y: float) -> tuple[float, float]:
        return self.transform().world_to_image(x, y)

    def image_to_world_xy(self, u: float, v: float) -> tuple[float, float]:
        return self.transform().image_to_world(u, v)

    def find_spawn(self, spawn_id: str) -> SpawnEdit | None:
        for spawn in self.spawns:
            if spawn.spawn_id == spawn_id:
                return spawn
        return None

    def find_route(self, route_id: str) -> RouteEdit | None:
        for route in self.routes:
            if route.route_id == route_id:
                return route
        return None


# ---------------------------------------------------------------------------
# 数值辅助
# ---------------------------------------------------------------------------


def _round(value: float, digits: int = 2) -> float | int:
    """YAML 输出用的数值：整数不带小数点，小数保留 digits 位。"""
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"数字必须是有限值: {value!r}")
    rounded = round(number, digits)
    return int(rounded) if float(rounded).is_integer() else rounded


def _float_or(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    number = float(value)
    return number if math.isfinite(number) else default


def _float_list(value: Any, length: int) -> tuple[float, ...] | None:
    if not isinstance(value, (list, tuple)) or len(value) != length:
        return None
    result: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        number = float(item)
        if not math.isfinite(number):
            return None
        result.append(number)
    return tuple(result)


# ---------------------------------------------------------------------------
# 加载
# ---------------------------------------------------------------------------


def _default_image_bounds(width: int, height: int) -> tuple[float, float, float, float]:
    return (0.0, 0.0, float(max(1, width)), float(max(1, height)))


def load_editor_state(map_dir: Path | str) -> MapEditorState:
    """载入地图包目录并构造可编辑状态；文件缺失/字段非法时使用默认值并记录提示。"""
    directory = Path(map_dir).expanduser()
    map_id = directory.name
    state = MapEditorState(map_dir=directory, map_id=map_id, name=map_id)
    background_path = directory / "background.png"
    if background_path.is_file():
        try:
            state.background = cc.read_image(background_path)
            state.background_path = background_path
        except ValueError as exc:
            state.load_messages.append(f"底图读取失败: {exc}")
    else:
        state.load_messages.append(f"底图不存在（待放入下载的地图）: {background_path}")
    width, height = state.image_size
    state.image_bounds = _default_image_bounds(width, height)

    map_yaml = directory / "map.yaml"
    if map_yaml.is_file():
        try:
            data = cc.read_yaml_mapping(map_yaml)
        except ValueError as exc:
            state.load_messages.append(f"map.yaml 读取失败，已用默认值: {exc}")
        else:
            state.name = str(data.get("name") or map_id)
            size = _float_list(data.get("size_meters"), 2)
            if size is not None:
                state.size_meters = (size[0], size[1])
            world_bounds = _float_list(data.get("world_bounds"), 4)
            if world_bounds is not None:
                state.world_bounds = (world_bounds[0], world_bounds[1], world_bounds[2], world_bounds[3])
            image_bounds = _float_list(data.get("image_bounds"), 4)
            if image_bounds is not None:
                state.image_bounds = (image_bounds[0], image_bounds[1], image_bounds[2], image_bounds[3])
            orientation_value = data.get("image_orientation")
            if isinstance(orientation_value, str):
                try:
                    state.orientation = ImageOrientation(orientation_value)
                except ValueError:
                    state.load_messages.append(
                        f"map.yaml 的 image_orientation 非法: {orientation_value!r}，已按 normal 处理"
                    )
            world_to_image = _float_list(data.get("world_to_image"), 6)
            state.world_to_image = list(world_to_image) if world_to_image is not None else None
            minimap_rect = _float_list(data.get("default_minimap_rect"), 4)
            state.default_minimap_rect = list(minimap_rect) if minimap_rect is not None else None
            state.notes = str(data.get("notes") or "")
            state.extra_map_fields = {
                str(key): value for key, value in data.items() if str(key) not in KNOWN_MAP_FIELDS
            }
    else:
        state.load_messages.append(f"map.yaml 不存在（将新建）: {map_yaml}")

    spawns_yaml = directory / "spawns.yaml"
    if spawns_yaml.is_file():
        try:
            spawn_data = cc.read_yaml_mapping(spawns_yaml)
        except ValueError as exc:
            state.load_messages.append(f"spawns.yaml 读取失败: {exc}")
        else:
            raw_spawns = spawn_data.get("spawns")
            if isinstance(raw_spawns, (list, tuple)):
                for item in raw_spawns:
                    spawn = SpawnEdit.from_yaml(item)
                    if spawn is not None:
                        state.spawns.append(spawn)

    routes_yaml = directory / "routes.yaml"
    if routes_yaml.is_file():
        try:
            route_data = cc.read_yaml_mapping(routes_yaml)
        except ValueError as exc:
            state.load_messages.append(f"routes.yaml 读取失败: {exc}")
        else:
            raw_routes = route_data.get("routes")
            if isinstance(raw_routes, (list, tuple)):
                for item in raw_routes:
                    route = RouteEdit.from_yaml(item)
                    if route is not None:
                        state.routes.append(route)
    return state


# ---------------------------------------------------------------------------
# 字段解析（GUI 输入框 -> 状态）
# ---------------------------------------------------------------------------


def apply_field_texts(
    state: MapEditorState,
    *,
    name: str,
    size_meters_text: str,
    world_bounds_text: str,
    image_bounds_text: str,
    orientation: ImageOrientation,
    notes: str,
) -> list[str]:
    """解析界面字段并写回 state；返回错误列表（有错误时不修改任何字段）。"""
    errors: list[str] = []
    try:
        size = cc.parse_number_pair(size_meters_text)
        if size[0] <= 0 or size[1] <= 0:
            raise ValueError(f"size_meters 必须为正数: {size!r}")
    except ValueError as exc:
        errors.append(f"size_meters: {exc}")
        size = state.size_meters
    try:
        world_bounds = cc.parse_number_quad(world_bounds_text)
        if world_bounds[2] <= world_bounds[0] or world_bounds[3] <= world_bounds[1]:
            raise ValueError(f"world_bounds 必须满足 x_max>x_min 且 y_max>y_min: {world_bounds!r}")
    except ValueError as exc:
        errors.append(f"world_bounds: {exc}")
        world_bounds = state.world_bounds
    try:
        image_bounds = cc.parse_number_quad(image_bounds_text)
        if image_bounds[2] <= image_bounds[0] or image_bounds[3] <= image_bounds[1]:
            raise ValueError(f"image_bounds 必须满足 u_max>u_min 且 v_max>v_min: {image_bounds!r}")
    except ValueError as exc:
        errors.append(f"image_bounds: {exc}")
        image_bounds = state.image_bounds
    cleaned_name = str(name).strip()
    if not cleaned_name:
        errors.append("name: 地图显示名称不能为空")
    if errors:
        return errors
    state.name = cleaned_name
    state.size_meters = (size[0], size[1])
    state.world_bounds = (world_bounds[0], world_bounds[1], world_bounds[2], world_bounds[3])
    state.image_bounds = (
        image_bounds[0],
        image_bounds[1],
        image_bounds[2],
        image_bounds[3],
    )
    state.orientation = orientation
    state.notes = str(notes).strip()
    return []


def apply_image_bounds_selection(state: MapEditorState, selection: cc.BoxSelection) -> None:
    """把拖框结果写入 image_bounds（像素坐标，保留浮点意义）。"""
    if selection.w <= 0 or selection.h <= 0:
        raise ValueError(f"框选区域为空: {selection!r}")
    state.image_bounds = (
        float(selection.x),
        float(selection.y),
        float(selection.right),
        float(selection.bottom),
    )


def add_spawn(state: MapEditorState, spawn: SpawnEdit) -> str | None:
    """添加出生点；编号重复时返回错误文本。"""
    if not spawn.spawn_id:
        return "出生点编号不能为空"
    if state.find_spawn(spawn.spawn_id) is not None:
        return f"出生点编号已存在: {spawn.spawn_id}"
    state.spawns.append(spawn)
    return None


def add_route(state: MapEditorState, route: RouteEdit) -> str | None:
    """添加航线；编号重复、出生点不存在时返回错误文本。"""
    if not route.route_id:
        return "航线编号不能为空"
    if state.find_route(route.route_id) is not None:
        return f"航线编号已存在: {route.route_id}"
    if state.find_spawn(route.spawn_id) is None:
        return f"航线绑定的出生点不存在: {route.spawn_id}"
    state.routes.append(route)
    return None


def append_waypoint(state: MapEditorState, route_id: str, x: float, y: float) -> str | None:
    """向指定航线追加一个世界坐标航点；航线不存在时返回错误文本。"""
    route = state.find_route(route_id)
    if route is None:
        return f"航线不存在: {route_id}"
    route.waypoints.append((float(x), float(y)))
    return None


def remove_spawn(state: MapEditorState, spawn_id: str, *, cascade_routes: bool = True) -> None:
    state.spawns = [spawn for spawn in state.spawns if spawn.spawn_id != spawn_id]
    if cascade_routes:
        state.routes = [route for route in state.routes if route.spawn_id != spawn_id]


def remove_route(state: MapEditorState, route_id: str) -> None:
    state.routes = [route for route in state.routes if route.route_id != route_id]


# ---------------------------------------------------------------------------
# 校验与保存
# ---------------------------------------------------------------------------


def build_map_pack(state: MapEditorState) -> MapPack:
    """把编辑状态转换成 ``MapPack``（校验与 ORB 预热共用同一份数据）。"""
    default_minimap_rect: NormalizedRect | None = None
    if state.default_minimap_rect is not None:
        try:
            default_minimap_rect = NormalizedRect.from_list(state.default_minimap_rect)
        except (TypeError, ValueError):
            default_minimap_rect = None
    background_path = state.background_path
    if background_path is not None and not background_path.is_file():
        background_path = None
    width, height = state.image_size
    return MapPack(
        map_id=state.map_id,
        name=state.name,
        size_meters=state.size_meters,
        world_bounds=state.world_bounds,
        image_bounds=state.image_bounds,
        image_orientation=state.orientation,
        world_to_image=tuple(state.world_to_image) if state.world_to_image is not None else None,
        default_minimap_rect=default_minimap_rect,
        notes=state.notes,
        pack_dir=state.map_dir,
        background_path=background_path,
        background_size=(width, height) if (width and height) else None,
        spawns=tuple(
            SpawnPoint(
                spawn_id=spawn.spawn_id,
                position=spawn.position,
                facing_deg=spawn.facing_deg,
                label=spawn.label,
            )
            for spawn in state.spawns
        ),
        routes=tuple(
            Route(
                route_id=route.route_id,
                spawn_id=route.spawn_id,
                waypoints=tuple(route.waypoints),
                weight=route.weight,
                description=route.description,
            )
            for route in state.routes
        ),
    )


def validate_editor_state(state: MapEditorState) -> list[ValidationIssue]:
    """校验编辑状态（详细设计 4.7 的全部地图包规则）。"""
    return validate_map_pack(build_map_pack(state), location=f"maps/{state.map_id}")


@dataclass(slots=True)
class SaveResult:
    """保存结果：是否写入、写入文件、校验问题。"""

    saved: bool
    files: tuple[Path, ...] = ()
    issues: tuple[ValidationIssue, ...] = ()

    @property
    def errors(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.is_error]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if not issue.is_error]


def map_yaml_payload(state: MapEditorState) -> dict[str, Any]:
    """map.yaml 的规范字段顺序；未知字段追加在末尾以保留手工编辑内容。"""
    payload: dict[str, Any] = {
        "map_id": state.map_id,
        "name": state.name,
        "size_meters": [_round(value) for value in state.size_meters],
        "world_bounds": [_round(value) for value in state.world_bounds],
        "image_bounds": [_round(value) for value in state.image_bounds],
        "image_orientation": state.orientation.value,
        "world_to_image": [_round(float(value), 4) for value in state.world_to_image]
        if state.world_to_image is not None
        else None,
        "default_minimap_rect": list(state.default_minimap_rect) if state.default_minimap_rect else None,
        "notes": state.notes,
    }
    for key, value in state.extra_map_fields.items():
        payload.setdefault(key, value)
    return payload


def spawns_yaml_payload(state: MapEditorState) -> dict[str, Any]:
    return {"spawns": [spawn.to_yaml() for spawn in state.spawns]}


def routes_yaml_payload(state: MapEditorState) -> dict[str, Any]:
    return {"routes": [route.to_yaml() for route in state.routes]}


def _write_yaml(path: Path, payload: dict[str, Any], default_header: str) -> Path:
    header = cc.read_leading_comments(path) or default_header
    return cc.write_yaml_mapping(path, payload, header=header)


def save_editor_state(state: MapEditorState) -> SaveResult:
    """校验并保存三个 YAML；有 error 时不写入任何文件。"""
    issues = validate_editor_state(state)
    if any(issue.is_error for issue in issues):
        return SaveResult(saved=False, issues=tuple(issues))
    directory = state.map_dir
    directory.mkdir(parents=True, exist_ok=True)
    files = (
        _write_yaml(directory / "map.yaml", map_yaml_payload(state), MAP_YAML_HEADER),
        _write_yaml(directory / "spawns.yaml", spawns_yaml_payload(state), SPAWNS_YAML_HEADER),
        _write_yaml(directory / "routes.yaml", routes_yaml_payload(state), ROUTES_YAML_HEADER),
    )
    return SaveResult(saved=True, files=files, issues=tuple(issues))


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------


class MapCalibrationApp:
    """地图标定主窗口（tkinter）。

    业务逻辑全部委托给本模块的纯函数；GUI 只负责收集输入、显示预览与提示。
    """

    def __init__(self, root: Any, state: MapEditorState) -> None:
        tkinter = cc.import_tkinter()
        from tkinter import messagebox, simpledialog  # noqa: PLC0415 - 延迟导入，无图形环境不影响 --help

        self._tk = tkinter
        self._messagebox = messagebox
        self._simpledialog = simpledialog
        self.root = root
        self.state = state
        self._pending: str | None = None
        self._selected_route_id: str | None = None
        self._build_ui()
        self._refresh_all()
        if state.load_messages:
            self._show_messages("载入提示", state.load_messages)

    # --- 界面搭建 -----------------------------------------------------
    def _build_ui(self) -> None:
        tk = self._tk
        main = tk.Frame(self.root)
        main.pack(fill="both", expand=True)
        left = tk.Frame(main, width=330)
        left.pack(side="left", fill="y", padx=6, pady=6)
        left.pack_propagate(False)

        tk.Label(left, text=f"地图编号: {self.state.map_id}", anchor="w", font=("", 10, "bold")).pack(
            fill="x", pady=(0, 6)
        )
        self._entries: dict[str, Any] = {}
        for key, label in (
            ("name", "地图名称"),
            ("size_meters", "size_meters（宽, 高，米）"),
            ("world_bounds", "world_bounds（x_min,y_min,x_max,y_max）"),
            ("image_bounds", "image_bounds（u_min,v_min,u_max,v_max）"),
        ):
            tk.Label(left, text=label, anchor="w").pack(fill="x")
            entry = tk.Entry(left)
            entry.pack(fill="x", pady=(0, 4))
            self._entries[key] = entry
        tk.Label(left, text="image_orientation（底图方向）", anchor="w").pack(fill="x")
        self._orientation_var = tk.StringVar(value=cc.orientation_label(self.state.orientation))
        orientation_box = tk.OptionMenu(
            left,
            self._orientation_var,
            *[cc.orientation_label(item) for item in ImageOrientation],
        )
        orientation_box.pack(fill="x", pady=(0, 4))
        tk.Label(left, text="notes（标定备注）", anchor="w").pack(fill="x")
        self._notes_entry = tk.Entry(left)
        self._notes_entry.pack(fill="x", pady=(0, 6))

        button_row = tk.Frame(left)
        button_row.pack(fill="x", pady=2)
        tk.Button(button_row, text="应用字段", command=self._apply_fields).pack(side="left", expand=True, fill="x")
        tk.Button(button_row, text="标可游玩区域", command=self._start_image_bounds).pack(
            side="left", expand=True, fill="x"
        )
        tk.Button(left, text="方向预览（正北朝上）", command=self._show_orientation_preview).pack(fill="x", pady=2)

        tk.Label(left, text="出生点", anchor="w").pack(fill="x", pady=(8, 0))
        self._spawn_list = tk.Listbox(left, height=6, exportselection=False)
        self._spawn_list.pack(fill="x")
        spawn_buttons = tk.Frame(left)
        spawn_buttons.pack(fill="x")
        tk.Button(spawn_buttons, text="添加出生点", command=self._start_add_spawn).pack(
            side="left", expand=True, fill="x"
        )
        tk.Button(spawn_buttons, text="删除选中出生点", command=self._delete_spawn).pack(
            side="left", expand=True, fill="x"
        )

        tk.Label(left, text="航线", anchor="w").pack(fill="x", pady=(8, 0))
        self._route_list = tk.Listbox(left, height=6, exportselection=False)
        self._route_list.pack(fill="x")
        self._route_list.bind("<<ListboxSelect>>", self._on_route_selected)
        route_buttons = tk.Frame(left)
        route_buttons.pack(fill="x")
        tk.Button(route_buttons, text="新建航线", command=self._create_route).pack(
            side="left", expand=True, fill="x"
        )
        tk.Button(route_buttons, text="添加航点", command=self._start_add_waypoint).pack(
            side="left", expand=True, fill="x"
        )
        tk.Button(left, text="删除选中航线", command=self._delete_route).pack(fill="x")
        tk.Button(left, text="校验（4.7 规则）", command=self._validate).pack(fill="x", pady=(8, 2))
        tk.Button(left, text="保存地图包", command=self._save).pack(fill="x")

        right = tk.Frame(main)
        right.pack(side="left", fill="both", expand=True, padx=6, pady=6)
        self.canvas = cc.ImageCanvas(right, on_box=self._on_box_selected, on_click=self._on_image_click)
        self.canvas.frame.pack(fill="both", expand=True)
        self._hint = tk.Label(right, anchor="w", fg="#995500")
        self._hint.pack(fill="x")

    # --- 状态刷新 -----------------------------------------------------
    def _refresh_all(self) -> None:
        self._refresh_fields()
        self._refresh_lists()
        self._refresh_annotations()
        if self.state.background is not None and self.canvas.image is None:
            self.canvas.set_image(self.state.background)

    def _refresh_fields(self) -> None:
        self._entries["name"].delete(0, "end")
        self._entries["name"].insert(0, self.state.name)
        self._entries["size_meters"].delete(0, "end")
        self._entries["size_meters"].insert(0, cc.format_numbers(self.state.size_meters))
        self._entries["world_bounds"].delete(0, "end")
        self._entries["world_bounds"].insert(0, cc.format_numbers(self.state.world_bounds))
        self._entries["image_bounds"].delete(0, "end")
        self._entries["image_bounds"].insert(0, cc.format_numbers(self.state.image_bounds))
        self._notes_entry.delete(0, "end")
        self._notes_entry.insert(0, self.state.notes)
        self._orientation_var.set(cc.orientation_label(self.state.orientation))

    def _refresh_lists(self) -> None:
        self._spawn_list.delete(0, "end")
        for spawn in self.state.spawns:
            self._spawn_list.insert("end", f"{spawn.spawn_id}  ({spawn.x:.0f}, {spawn.y:.0f})")
        self._route_list.delete(0, "end")
        for route in self.state.routes:
            self._route_list.insert(
                "end",
                f"{route.route_id} <- {route.spawn_id}  航点 {len(route.waypoints)}  权重 {route.weight:g}",
            )

    def _refresh_annotations(self) -> None:
        from .calibrate_common import Annotation

        annotations: list[Annotation] = []
        try:
            transform = self.state.transform()
        except (ValueError, ZeroDivisionError):
            transform = None
        if transform is not None:
            u_min, v_min, u_max, v_max = self.state.image_bounds
            annotations.append(
                Annotation(
                    kind="line",
                    points=((u_min, v_min), (u_max, v_min), (u_max, v_max), (u_min, v_max), (u_min, v_min)),
                    color="#3388ff",
                )
            )
            spawn_points: list[tuple[float, float]] = []
            for spawn in self.state.spawns:
                point = transform.world_to_image(spawn.x, spawn.y)
                spawn_points.append(point)
                annotations.append(
                    Annotation(kind="point", points=(point,), color="#00dd66", text=spawn.spawn_id)
                )
            for route in self.state.routes:
                points = [transform.world_to_image(x, y) for x, y in route.waypoints]
                spawn = self.state.find_spawn(route.spawn_id)
                if spawn is not None:
                    points.insert(0, transform.world_to_image(spawn.x, spawn.y))
                if len(points) >= 2:
                    color = "#ffaa00" if route.route_id == self._selected_route_id else "#00bbff"
                    annotations.append(Annotation(kind="line", points=tuple(points), color=color))
                    if route.route_id == self._selected_route_id:
                        for waypoint in route.waypoints:
                            annotations.append(
                                Annotation(
                                    kind="point",
                                    points=(transform.world_to_image(*waypoint),),
                                    color="#ffaa00",
                                    radius=3,
                                )
                            )
        self.canvas.set_annotations(annotations)

    def _on_route_selected(self, _event: Any = None) -> None:
        selection = self._route_list.curselection()
        if selection:
            route = self.state.routes[int(selection[0])]
            self._selected_route_id = route.route_id
            self._refresh_annotations()

    # --- 字段与操作 ---------------------------------------------------
    def _apply_fields(self, *, show_errors: bool = True) -> bool:
        try:
            orientation = cc.orientation_from_label(self._orientation_var.get())
        except ValueError:
            orientation = self.state.orientation
        errors = apply_field_texts(
            self.state,
            name=self._entries["name"].get(),
            size_meters_text=self._entries["size_meters"].get(),
            world_bounds_text=self._entries["world_bounds"].get(),
            image_bounds_text=self._entries["image_bounds"].get(),
            orientation=orientation,
            notes=self._notes_entry.get(),
        )
        if errors:
            if show_errors:
                self._show_messages("字段错误", errors, error=True)
            return False
        self._refresh_annotations()
        return True

    def _set_pending(self, pending: str | None, hint: str = "") -> None:
        self._pending = pending
        if pending == "image_bounds":
            self.canvas.set_mode(cc.ImageCanvas.MODE_BOX)
        elif pending in {"spawn", "waypoint"}:
            self.canvas.set_mode(cc.ImageCanvas.MODE_CLICK)
        else:
            self.canvas.set_mode(cc.ImageCanvas.MODE_PAN)
        self._hint.configure(text=hint)

    def _start_image_bounds(self) -> None:
        self._set_pending("image_bounds", "在底图上拖动矩形标出可游玩区域（不含图例/边框）")

    def _start_add_spawn(self) -> None:
        if not self._apply_fields():
            return
        if self.state.background is None:
            self._show_messages("无法添加出生点", ["尚未载入底图"], error=True)
            return
        self._set_pending("spawn", "在底图上单击出生点位置（世界坐标由 image_bounds 换算）")

    def _start_add_waypoint(self) -> None:
        if not self._apply_fields():
            return
        if self._selected_route_id is None:
            self._show_messages("无法添加航点", ["请先在航线列表中选择一条航线"], error=True)
            return
        self._set_pending("waypoint", f"在底图上单击添加航点（当前航线 {self._selected_route_id}）")

    def _create_route(self) -> None:
        if not self._apply_fields():
            return
        if not self.state.spawns:
            self._show_messages("无法新建航线", ["请先添加至少一个出生点"], error=True)
            return
        route_id = self._simpledialog.askstring("新建航线", "航线编号（英文小写与下划线）:")
        if not route_id:
            return
        default_spawn = self.state.spawns[0].spawn_id
        spawn_id = self._simpledialog.askstring("新建航线", f"绑定出生点编号（已有: "
                                                   f"{', '.join(s.spawn_id for s in self.state.spawns)}）:",
                                                   initialvalue=default_spawn)
        if not spawn_id:
            return
        weight_text = self._simpledialog.askstring("新建航线", "选择权重（默认 1.0）:", initialvalue="1.0")
        try:
            weight = cc.parse_number(weight_text) if weight_text else 1.0
        except ValueError as exc:
            self._show_messages("权重非法", [str(exc)], error=True)
            return
        error = add_route(
            self.state,
            RouteEdit(route_id=route_id.strip(), spawn_id=spawn_id.strip(), weight=weight),
        )
        if error:
            self._show_messages("无法新建航线", [error], error=True)
            return
        self._selected_route_id = route_id.strip()
        self._refresh_all()

    def _delete_spawn(self) -> None:
        selection = self._spawn_list.curselection()
        if not selection:
            return
        spawn = self.state.spawns[int(selection[0])]
        if not self._messagebox.askyesno(
            "删除出生点", f"删除出生点 {spawn.spawn_id} 以及绑定它的航线？"
        ):
            return
        remove_spawn(self.state, spawn.spawn_id)
        self._refresh_all()

    def _delete_route(self) -> None:
        selection = self._route_list.curselection()
        if not selection:
            return
        route = self.state.routes[int(selection[0])]
        if not self._messagebox.askyesno("删除航线", f"删除航线 {route.route_id}？"):
            return
        remove_route(self.state, route.route_id)
        if self._selected_route_id == route.route_id:
            self._selected_route_id = None
        self._refresh_all()

    # --- 画布回调 -----------------------------------------------------
    def _on_box_selected(self, selection: cc.BoxSelection) -> None:
        if self._pending != "image_bounds":
            return
        apply_image_bounds_selection(self.state, selection)
        self._set_pending(None, "")
        self._refresh_fields()
        self._refresh_annotations()

    def _on_image_click(self, point: tuple[float, float]) -> None:
        if self._pending not in {"spawn", "waypoint"}:
            return
        try:
            world_x, world_y = self.state.image_to_world_xy(point[0], point[1])
        except (ValueError, ZeroDivisionError) as exc:
            self._show_messages("坐标换算失败", [f"请先正确设置 world_bounds/image_bounds: {exc}"], error=True)
            return
        if self._pending == "spawn":
            spawn_id = self._simpledialog.askstring(
                "添加出生点",
                f"世界坐标 ({world_x:.0f}, {world_y:.0f})\n出生点编号（英文小写与下划线）:",
                initialvalue=f"spawn_{len(self.state.spawns) + 1}",
            )
            if not spawn_id:
                return
            facing_text = self._simpledialog.askstring("添加出生点", "初始朝向（0~360 度，正北为 0）:",
                                                          initialvalue="0")
            try:
                facing = cc.parse_number(facing_text) if facing_text else 0.0
            except ValueError as exc:
                self._show_messages("朝向非法", [str(exc)], error=True)
                return
            label = self._simpledialog.askstring("添加出生点", "说明（可留空）:", initialvalue="") or ""
            error = add_spawn(
                self.state,
                SpawnEdit(spawn_id=spawn_id.strip(), x=world_x, y=world_y, facing_deg=facing, label=label),
            )
            if error:
                self._show_messages("无法添加出生点", [error], error=True)
                return
            self._refresh_all()
        else:
            route_id = self._selected_route_id
            if route_id is None:
                self._show_messages("无法添加航点", ["请先在航线列表中选择航线"], error=True)
                self._set_pending(None, "")
                return
            error = append_waypoint(self.state, route_id, world_x, world_y)
            if error:
                self._show_messages("无法添加航点", [error], error=True)
                return
            self._refresh_all()

    # --- 预览 / 校验 / 保存 -------------------------------------------
    def _show_orientation_preview(self) -> None:
        if self.state.background is None:
            self._show_messages("方向预览", ["尚未载入底图"], error=True)
            return
        if not self._apply_fields():
            return
        try:
            preview = cc.canonicalize_image(
                self.state.background, self.state.image_bounds, self.state.orientation
            )
            matrix = cc.image_to_canonical_affine(self.state.image_bounds, self.state.orientation)
        except ValueError as exc:
            self._show_messages("方向预览失败", [str(exc)], error=True)
            return
        from ..common.coords import affine_to_matrix, apply_affine

        matrix_3x3 = affine_to_matrix(
            (
                matrix[0, 0],
                matrix[0, 1],
                matrix[0, 2],
                matrix[1, 0],
                matrix[1, 1],
                matrix[1, 2],
            )
        )
        transform = self.state.transform()
        annotations: list[cc.Annotation] = []
        for spawn in self.state.spawns:
            u, v = transform.world_to_image(spawn.x, spawn.y)
            x, y = apply_affine(matrix_3x3, u, v)
            annotations.append(cc.Annotation(kind="point", points=((x, y),), color="#00dd66", text=spawn.spawn_id))
        for route in self.state.routes:
            points = []
            spawn = self.state.find_spawn(route.spawn_id)
            if spawn is not None:
                points.append(transform.world_to_image(spawn.x, spawn.y))
            points.extend(transform.world_to_image(x, y) for x, y in route.waypoints)
            mapped = [apply_affine(matrix_3x3, u, v) for u, v in points]
            if len(mapped) >= 2:
                annotations.append(cc.Annotation(kind="line", points=tuple(mapped), color="#ffaa00"))
        window = self._tk.Toplevel(self.root)
        window.title("方向预览（正北朝上）")
        viewer = cc.ImageCanvas(window)
        viewer.frame.pack(fill="both", expand=True)
        viewer.set_image(preview)
        viewer.set_annotations(annotations)
        self._tk.Label(
            window,
            text=f"当前方向: {cc.orientation_label(self.state.orientation)}；"
            f"绿色=出生点，橙色=航线；请与游戏内小地图对照确认方向无误",
            anchor="w",
        ).pack(fill="x")

    def _validate(self) -> None:
        if not self._apply_fields():
            return
        issues = validate_editor_state(self.state)
        lines = [issue.format() for issue in issues] or ["校验通过，没有错误或警告"]
        self._show_messages("地图包校验", lines, error=any(issue.is_error for issue in issues))

    def _save(self) -> None:
        if not self._apply_fields():
            return
        issues = validate_editor_state(self.state)
        errors = [issue for issue in issues if issue.is_error]
        if errors:
            self._show_messages(
                "校验失败，未保存",
                [issue.format() for issue in errors],
                error=True,
            )
            return
        warnings = [issue for issue in issues if not issue.is_error]
        if warnings and not self._messagebox.askyesno(
            "存在警告",
            "\n".join(issue.format() for issue in warnings) + "\n\n是否继续保存？",
        ):
            return
        result = save_editor_state(self.state)
        if not result.saved:
            self._show_messages("保存失败", [issue.format() for issue in result.errors], error=True)
            return
        lines = [f"已保存: {path}" for path in result.files]
        if warnings:
            lines.append(f"保存时有 {len(warnings)} 个警告")
        self._show_messages("保存成功", lines)

    # --- 提示 ---------------------------------------------------------
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
        prog="python -m src.tools.calibrate_map",
        description="地图标定 GUI：标 image_bounds/world_bounds、方向、出生点与航线，保存并校验地图包",
    )
    parser.add_argument("--map-dir", default=None, help="地图包目录，例如 maps/north_sea")
    parser.add_argument("--root", default=".", help="项目根目录（配合 --map-id 使用），默认当前目录")
    parser.add_argument("--map-id", default=None, help="地图编号（与 --root 组合定位 maps/<map_id>/）")
    return parser


def resolve_map_dir(args: argparse.Namespace) -> Path:
    """把 --map-dir / --root+--map-id 解析为地图目录。"""
    if args.map_dir:
        directory = Path(args.map_dir).expanduser()
        if not directory.is_absolute():
            directory = Path(args.root).expanduser().resolve() / directory
        return directory
    if args.map_id:
        return Path(args.root).expanduser().resolve() / "maps" / args.map_id
    raise ValueError("需要 --map-dir，或同时给出 --root 与 --map-id")


def main(argv: list[str] | None = None) -> int:
    """地图标定入口：载入地图包并打开 GUI（不在此处自动创建目录）。"""
    args = _build_parser().parse_args(argv)
    try:
        map_dir = resolve_map_dir(args)
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    if not map_dir.is_dir():
        print(f"[ERROR] 地图包目录不存在: {map_dir}（请先下载 background.png 并建立目录）", file=sys.stderr)
        return 2
    state = load_editor_state(map_dir)
    try:
        root = cc.create_root(f"地图标定 - {map_dir.name}")
    except RuntimeError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    MapCalibrationApp(root, state)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
