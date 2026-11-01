"""配置与地图包数据模型（详细设计 3 / 4 / 6.8 / 12）。

模型只描述数据结构与默认值，不读文件、不校验引用；加载与校验在
``src.config.loader`` / ``src.config.validate`` 中实现。

与设计文档的差异（配置项补充口径）记录在
``design/详细设计评审与实现补充.md``：正文引用但 3.2 / 3.3 字段表未列出的参数，
在这里定义为显式字段并给出默认值。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..common.coords import ImageOrientation, WorldImageTransform
from ..common.types import NormalizedRect

# ---------------------------------------------------------------------------
# 客户端配置（详细设计 3.2）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FeatureDef:
    """界面特征：模板图片 + 归一化搜索区域 + 阈值 + 缩放范围。"""

    feature_id: str
    template: str
    region: NormalizedRect
    threshold: float = 0.85
    scales: tuple[float, ...] = (0.95, 1.0, 1.05)
    template_path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature_id": self.feature_id,
            "template": self.template,
            "region": self.region.to_list(),
            "threshold": self.threshold,
            "scales": list(self.scales),
        }


@dataclass(frozen=True, slots=True)
class StepDef:
    """步骤序列中的一个步骤（详细设计 10.1）。

    ``action`` 取值：key / click_feature / wait_feature / wait_feature_absent /
    wait_ms / scroll。
    ``scroll`` 步骤用于船只列表滚动（详细设计 6.8）：在 ``region`` 内滚动
    ``scroll_clicks`` 格，正数向上、负数向下。
    ``click_feature`` 可带像素偏移 ``offset_x/offset_y``：模板常包含周围背景，
    点击点按偏移修正到按钮中心。
    """

    action: str
    key: str | None = None
    feature: str | None = None
    timeout_ms: int = 3000
    retries: int = 1
    optional: bool = False
    wait_after_ms: int = 0
    duration_ms: int = 0
    scroll_clicks: int = 0
    region: NormalizedRect | None = None
    offset_x: int = 0
    offset_y: int = 0
    interval_ms: int = 300


@dataclass(frozen=True, slots=True)
class WindowConfig:
    process_names: tuple[str, ...] = ()
    title_patterns: tuple[str, ...] = ()
    require_foreground: bool = True


@dataclass(frozen=True, slots=True)
class MinimapConfig:
    """小地图标定（详细设计 5.6）。"""

    rect: NormalizedRect
    border_px: int = 0
    frame_feature: str | None = None
    marker_template: str | None = None
    marker_angles: int = 24
    # 自身标记颜色过滤范围，HSV 三个分量，None 表示不做颜色过滤
    marker_hsv_lower: tuple[int, int, int] | None = None
    marker_hsv_upper: tuple[int, int, int] | None = None
    marker_jump_px: float = 300.0
    marker_lost_s: float = 3.0


@dataclass(frozen=True, slots=True)
class BattleHudConfig:
    """战斗 HUD 与死亡判定相关的界面标定（详细设计 5.9 / 评审 10.2）。"""

    hp_bar_region: NormalizedRect | None = None
    hp_fill_hsv_lower: tuple[int, int, int] | None = None
    hp_fill_hsv_upper: tuple[int, int, int] | None = None
    hp_fill_zero_ratio: float = 0.03
    # --- ISS-002（评审 10.2）：颜色无关的血条填充列比例判定 ---------------
    # hp_fill_zero_ratio / hp_fill_hsv_* 降级为辅助证据（保留兼容，不再单独代表血量）：
    # 整条血条区域逐列统计“填充像素”，fill ratio = 填充列数 / 有效列数。
    # 低于 empty_max 视为空条（确认归零），高于 alive_min 视为存活，两者之间无法判断。
    hp_bar_empty_max_ratio: float = 0.02
    hp_bar_alive_min_ratio: float = 0.10
    # 退出授权需要连续 N 帧确认归零，且最近证据在 TTL 内（超时/矛盾证据立即撤销）。
    hp_bar_confirm_frames: int = 2
    hp_bar_evidence_ttl_s: float = 1.0
    spectator_features: tuple[str, ...] = ()
    death_banner_feature: str = "death_banner"
    result_feature: str = "battle_results"
    # 结算页按钮（2026-10-31 用户口径）：优先“继续战斗！”直接下一局，没有时再“回到港口”
    result_continue_feature: str | None = None
    result_return_feature: str | None = "result_return_port_button"


@dataclass(frozen=True, slots=True)
class CameraConfig:
    """视角控制（详细设计 8.4 / 9.6）。"""

    relative: bool = True
    sensitivity: float = 1.0


@dataclass(frozen=True, slots=True)
class ExitConfig:
    """退出战斗的按键与界面特征（详细设计 10.2 / 10.7）。

    设计文档 6.1 的 EXIT_* 状态与 10.1 的步骤序列存在职责重叠，实现口径：
    退出流程由状态机驱动，客户端配置只提供退出按键和菜单/按钮/确认框/港口特征；
    步骤序列执行器用于开局、船只选择与弹窗清理（见实现补充文档）。
    """

    menu_key: str = "escape"
    menu_open_feature: str | None = None
    return_port_feature: str | None = None
    confirm_feature: str | None = None
    port_feature: str = "port_indicator"
    direct_exit_feature: str | None = None
    post_click_wait_ms: int = 500


@dataclass(frozen=True, slots=True)
class ClientProfile:
    """一个客户端版本的配置（详细设计 3.2）。"""

    client_id: str
    display_name: str
    window: WindowConfig
    language: str
    reference_resolution: tuple[int, int]
    features: Mapping[str, FeatureDef]
    start_sequence: tuple[StepDef, ...] = ()
    start_sequence_full: tuple[StepDef, ...] = ()
    popup_sequence: tuple[StepDef, ...] = ()
    result_sequence: tuple[StepDef, ...] = ()
    ship_selection_sequence: tuple[StepDef, ...] = ()
    # 每场战斗前在港口执行的“装备页信号旗校验”步骤：切到装备页 → 等待空信号旗特征 → 切回港口页
    ship_check_sequence: tuple[StepDef, ...] = ()
    ship_verify_features: tuple[str, ...] = ()
    loadout_verify_features: tuple[str, ...] = ()
    clear_loadout_sequence: tuple[StepDef, ...] | None = None
    keys_file: str = "keys.yaml"
    minimap: MinimapConfig | None = None
    battle_hud: BattleHudConfig = field(default_factory=BattleHudConfig)
    camera: CameraConfig = field(default_factory=CameraConfig)
    exit: ExitConfig = field(default_factory=ExitConfig)
    # 状态 -> 界面特征（第一个为主判定，其余为辅助）；缺省时使用 perception 内置表
    state_features: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    # 回港后可能出现的弹窗面板特征
    popup_features: tuple[str, ...] = ()
    profile_dir: Path | None = None
    notes: str = ""

    def feature(self, feature_id: str) -> FeatureDef | None:
        return self.features.get(feature_id)

    def state_feature_ids(self, state: str) -> tuple[str, ...]:
        return self.state_features.get(state, ())


@dataclass(frozen=True, slots=True)
class ConsumableSlot:
    key: str
    name: str = ""


@dataclass(frozen=True, slots=True)
class KeyMap:
    """按键映射（详细设计 3.4）。配置只出现可读名称，虚拟键码在输入模块维护。"""

    actions: Mapping[str, str] = field(default_factory=dict)
    camera_relative: bool = True
    consumables: tuple[ConsumableSlot, ...] = ()
    camera_sensitivity: float = 1.0

    def resolve(self, name: str) -> str:
        """把逻辑动作名或原始按键名解析为按键名。"""
        return self.actions.get(name, name)

    def key(self, action: str) -> str:
        if action not in self.actions:
            raise KeyError(f"keys.yaml 缺少动作映射: {action}")
        return self.actions[action]

    @property
    def escape(self) -> str:
        return self.key("escape")

    @property
    def fire(self) -> str:
        return self.key("fire")

    @property
    def full_map(self) -> str:
        return self.key("full_map")


@dataclass(frozen=True, slots=True)
class LoadoutSlotDef:
    """空装备槽校验配置（详细设计 6.8）。"""

    slot_id: str
    name: str
    empty_template: str
    region: NormalizedRect | None = None
    template_path: Path | None = None


@dataclass(frozen=True, slots=True)
class ShipProfile:
    """船只卡片、已选中证据与选船查找参数（详细设计 3.2 / 6.8 / 实现补充 10.7）。

    ISS-007 口径：目标船卡片“可见”不等于“已选中”。选中态由 :attr:`selected_feature`
    （目标船处于选中状态时的模板，由标定工具生成）独立确认；未标定或无法可靠匹配时
    运行期保守失败并提示补标定，不能把 ``ship_card`` 命中当作当前船正确。
    """

    ship_id: str
    display_name: str
    card_template: str = "card.png"
    card_search_region: NormalizedRect | None = None
    # --- 已选中证据（ISS-007 / 实现补充 10.7） -------------------------------
    # 选中态模板文件名（相对 ship.yaml 目录），例如 selected.png。
    # 模板必须同时包含目标船身份特征（船名/船体）与“已选中”高亮/边框，不能只截通用
    # 选中边框（否则任何船被选中都会误匹配）；由标定工具生成。
    # None 或模板文件缺失表示尚未标定：运行期 FAILED 并提示补标定，不做猜测。
    selected_feature: str | None = None
    # 选中态模板搜索区域；None 时由 loader 回退为 card_search_region
    selected_region: NormalizedRect | None = None
    selected_threshold: float = 0.78
    # 右侧详情面板“当前选中船”船名模板（ISS-007 实机补充 2026-10-31）：
    # 港口右侧面板指挥官上方显示当前选中船名，与卡片背景/高亮无关，比 selected_feature 更稳；
    # 两者任一命中即视为已选中。模板缺失时 validate 只告警，不阻断旧流程。
    selected_name_feature: str | None = None
    selected_name_region: NormalizedRect | None = None
    selected_name_threshold: float = 0.78
    # 每船独立的“己方血条存在”模板：不同船名/舰种的血条面板文字不同，换船需替换；
    # 未提供时死亡判定不使用“血条消失”信号（改用死亡提示/结算界面兜底）。
    hp_bar_template: str | None = None
    loadout_preset_feature: str | None = None
    loadout_slots: tuple[LoadoutSlotDef, ...] = ()
    # --- 选船查找（ISS-007）：所有参数来自 ship.yaml，不写死在代码里 ---------
    max_scrolls: int = 20  # 扫描阶段最大滚动次数（空 ship_selection_sequence 也使用该预算）
    scroll_direction: str = "down"  # 船只列表滚动方向：down 或 up（详细设计 6.8）
    scroll_clicks: int = 1  # 每次滚动格数
    cover_list_start: bool = True  # 先反向滚到可确认的列表起点再扫描，覆盖目标在初始位置两侧
    max_position_scrolls: int = 10  # 起点定位阶段最大滚动次数
    scroll_settle_s: float = 0.4  # 每次滚动后等待新画面/稳定的时间
    confirm_frames: int = 2  # 点击目标卡后至少等待的新帧数，才查询已选中证据
    confirm_timeout_s: float = 3.0  # 点击后等待新帧并确认已选中的超时
    max_click_attempts: int = 2  # 同一目标重复点击上限（避免每场都点与无限点击）
    endpoint_stable_scrolls: int = 2  # 连续“滚动后画面不再变化”次数，用于判定列表端点
    endpoint_diff_ratio: float = 0.02  # 画面采样平均差异低于该比例视为“没有变化”
    candidate_merge_px: int = 8  # 候选中心距离小于该像素视为同一张卡，避免重复候选
    selection_timeout_s: float = 60.0  # 整个选船查找（定位+扫描+确认）的总超时
    notes: str = ""
    ship_dir: Path | None = None
    card_path: Path | None = None
    hp_bar_path: Path | None = None
    selected_path: Path | None = None
    selected_name_path: Path | None = None


# ---------------------------------------------------------------------------
# 地图包（详细设计 4）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SpawnPoint:
    spawn_id: str
    position: tuple[float, float]
    facing_deg: float = 0.0
    label: str = ""


@dataclass(frozen=True, slots=True)
class Route:
    route_id: str
    spawn_id: str
    waypoints: tuple[tuple[float, float], ...]
    weight: float = 1.0
    description: str = ""


@dataclass(frozen=True, slots=True)
class MapPack:
    """一张地图的完整数据包（详细设计 4.1）。"""

    map_id: str
    name: str
    size_meters: tuple[float, float]
    world_bounds: tuple[float, float, float, float]
    image_bounds: tuple[float, float, float, float]
    image_orientation: ImageOrientation = ImageOrientation.NORMAL
    world_to_image: tuple[float, ...] | None = None
    default_minimap_rect: NormalizedRect | None = None
    notes: str = ""
    pack_dir: Path | None = None
    background_path: Path | None = None
    background_size: tuple[int, int] | None = None
    spawns: tuple[SpawnPoint, ...] = ()
    routes: tuple[Route, ...] = ()

    # --- 派生对象 ---------------------------------------------------
    def transform(self) -> WorldImageTransform:
        return WorldImageTransform(
            world_bounds=self.world_bounds,
            image_bounds=self.image_bounds,
            orientation=self.image_orientation,
            world_to_image_matrix=self.world_to_image,  # type: ignore[arg-type]
        )

    # --- 查询辅助 ---------------------------------------------------
    def spawn(self, spawn_id: str) -> SpawnPoint | None:
        for item in self.spawns:
            if item.spawn_id == spawn_id:
                return item
        return None

    def routes_for(self, spawn_id: str) -> tuple[Route, ...]:
        return tuple(route for route in self.routes if route.spawn_id == spawn_id)

    def route(self, route_id: str) -> Route | None:
        for item in self.routes:
            if item.route_id == route_id:
                return item
        return None


# ---------------------------------------------------------------------------
# 全局配置（详细设计 3.3）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    loop_hz: int = 10
    capture_hz: float = 2.0
    debug: bool = False
    log_level: str = "INFO"


@dataclass(frozen=True, slots=True)
class PathsConfig:
    data_dir: Path = Path("data")
    log_dir: Path = Path("logs")
    screenshot_dir: Path = Path("screenshots")
    maps_dir: Path = Path("maps")
    profiles_dir: Path = Path("profiles")


@dataclass(frozen=True, slots=True)
class MapConfig:
    auto_identify: bool = True
    forced_map_id: str | None = None
    spawn_tolerance_m: float = 500.0
    default_spawn_id: str | None = None
    # 期望的小地图缩放比例（底图正北画面像素 -> 小地图内容区像素）；
    # null 表示按 image_bounds 与小地图内容区尺寸自动推导（详细设计 5.5）
    expected_minimap_scale: float | None = None


@dataclass(frozen=True, slots=True)
class NavigationConfig:
    """导航参数。判定阈值以像素为准（评审 P0-5）：
    小地图约 40 米/像素、截屏 2 Hz，米制阈值 20 m=0.5 px、80 m=2 px 低于定位噪声，
    因此移动确认与卡住判定使用 px；arrival_m/overshoot_m 只用世界距离做上限。
    """

    # 点击面（ISS-001，实现补充 10.1）：full_map = 按 M 打开全屏地图后点选，是 autopilot 的
    # 运行默认（项目 config.yaml 取此值）；minimap = 旧“直接点小地图”交互，仅作失败降级/兼容保留。
    # 模型默认保留 minimap：waypoints 旧模式与不写 navigation 段的测试配置仍走旧行为。
    surface: str = "minimap"
    # 导航模式（用户简化需求）：autopilot = 开局点小地图敌区后交给游戏自动驾驶，不碰 WASD；
    # waypoints = 使用地图包出生点/航线逐点导航（需要地图识别与标定）
    mode: str = "autopilot"
    # 敌区目标：center 或 symmetric（我方出生点关于地图中心的对称点，x 固定到中线）
    autopilot_target: str = "symmetric"
    autopilot_reclick_s: tuple[float, float] = (60.0, 120.0)  # 重发航点间隔（到达/卡住自动重发）
    autopilot_overshoot: float = 0.6  # waypoints 旧模式预留；autopilot 不使用
    autopilot_offset: tuple[float, float] = (0.0, 0.0)  # 目标相对归一化坐标的微调
    arrival_m: float = 400.0
    overshoot_m: float = 800.0
    overshoot_cos_threshold: float = -0.5
    click_retry: int = 2
    click_confirm_first_s: float = 8.0
    click_confirm_s: float = 5.0
    move_confirm_px: float = 3.0
    click_min_interval_s: float = 4.0
    retry_interval_s: float = 4.0
    stuck_window_s: float = 20.0
    stuck_distance_px: float = 3.0
    max_skipped: int = 3
    ship_speed_window_s: float = 4.0
    ship_speed_samples: int = 9
    ship_lost_degrade_s: float = 3.0
    ship_lost_abandon_s: float = 10.0
    route_end_action: str = "wait"  # v1 只支持 wait
    full_map_rect: NormalizedRect | None = None
    full_map_toggle_key: str = "full_map"
    # --- ISS-001：M 全屏地图点选时序与验证（实现补充 10.1；默认值需实机确认）---
    full_map_open_wait_s: float = 1.0  # 按 M 后等待地图界面就绪（固定时序假设）
    full_map_close_wait_s: float = 0.4  # 点击生效后等待再按 M 关图
    full_map_verify_s: float = 8.0  # 关图后验证航向生效的观察窗口
    full_map_retry: int = 2  # 验证失败后的全屏地图重试次数（不含首次）
    autopilot_verify_min_move_px: float = 2.0  # 验证成功的最小自身标记位移（minimap_px）
    autopilot_verify_min_speed: float = 0.3  # 验证成功的最小船速（m/s）
    # ISS-001 实机修复：优先用战斗 HUD 的“M 自动驾驶开启”特征确认航向已生效
    # （实机中船速估计可能为 0、小地图 8s 位移不足 3px，会导致误判失败并降级）
    autopilot_verify_feature: str | None = "autopilot_indicator"


@dataclass(frozen=True, slots=True)
class BehaviorConfig:
    camera_interval_s: tuple[float, float] = (3.0, 15.0)
    camera_mean_s: float = 8.0
    camera_sd_s: float = 2.8  # 0.35×均值；截断正态采样，避免钳位尖峰
    attack_interval_s: tuple[float, float] = (25.0, 120.0)
    attack_mean_s: float = 60.0
    attack_sd_s: float = 21.0  # 0.35×均值
    consumable_ratio: float = 0.15
    consumable_ratio_range: tuple[float, float] = (0.05, 0.25)
    camera_activity_range: tuple[float, float] = (0.6, 1.4)
    attack_frequency_range: tuple[float, float] = (0.5, 1.5)
    long_stare_bias_range: tuple[float, float] = (0.0, 1.0)
    long_stare_probability: float = 0.15
    long_stare_s: tuple[float, float] = (5.0, 20.0)
    move_px: tuple[int, int] = (20, 160)
    move_steps: tuple[int, int] = (3, 6)
    move_duration_ms: tuple[int, int] = (200, 800)
    fire_sector_deg: float = 60.0
    fire_turn_probability: float = 0.5
    fire_burst: tuple[int, int] = (1, 4)
    fire_interval_ms: tuple[int, int] = (300, 1100)
    fire_click_region: NormalizedRect = field(
        default_factory=lambda: NormalizedRect(0.30, 0.30, 0.70, 0.70)
    )
    consumable_max_per_battle: int = 10
    activity_watchdog_s: tuple[float, float] = (35.0, 95.0)
    # 按输入事件计数（含视角拆分步与点射发数），防止配置错误导致高频输入
    actions_per_minute_max: int = 40
    battle_start_quiet_s: float = 10.0
    # 行为输入前允许的感知帧最大年龄；超过则跳过本次动作（防止在过期画面上输入）
    max_input_frame_age_s: float = 1.0


@dataclass(frozen=True, slots=True)
class ShipConfig:
    ship_id: str = ""
    verify_each_battle: bool = True


@dataclass(frozen=True, slots=True)
class SafetyConfig:
    emergency_stop_key: str = "f12"
    resume_key: str = "f11"
    max_session_minutes: float = 240.0
    max_battles_per_session: int = 20
    pause_when_unfocused: bool = True
    heartbeat_s: float = 10.0
    topmost: bool = False
    auto_restart_game: bool = False
    restart_wait_s: float = 30.0
    restart_max_attempts: int = 2


@dataclass(frozen=True, slots=True)
class SessionConfig:
    between_battle_s: tuple[float, float] = (3.0, 10.0)
    daily_battle_cap: int = 30
    allowed_hours: tuple[str, str] | None = None
    max_consecutive_start_failures: int = 2
    recovery_max_per_10min: int = 3


@dataclass(frozen=True, slots=True)
class OverloadConfig:
    """性能采样与过载保护（GUI 设计 §6 / 2026-10-31 确认阈值）。

    只描述参数，不读系统指标、不做判定；采样、状态机与事件分别位于
    ``src.common.sysmetrics``、``src.execution.overload_policy``、``src.execution.overload``。

    默认 ``mode="monitor"``：只采样并在界面记录“本应暂停/停止”，不实际暂停或停止。
    所有 MB 阈值为“余量下限”或“占用上限”，实际生效值 = max(下限, 比例 × 总量/上限)：
    这样 16GB/32GB/64GB 机器都按同一相对口径提前预警。阈值全部来自配置，不写死在代码里。
    """

    #: off=关闭；monitor=只记录；pause_stop=先暂停、必要时停止；stop=达到条件直接停止
    mode: str = "monitor"
    #: 采样频率（1 Hz；窗口覆盖率按该频率估算）
    sample_hz: float = 1.0
    #: 判定持续窗口（秒）
    window_warn_s: float = 60.0
    window_long_s: float = 120.0
    window_stop_after_pause_s: float = 180.0
    window_resume_s: float = 60.0
    #: 窗口有效覆盖率下限（样本缺失/首拍/越界丢弃后低于该值不判定）
    min_coverage: float = 0.9
    # --- W 告警（只记录，不动作） --------------------------------------------
    cpu_warn_pct: float = 90.0
    physical_warn_floor_mb: float = 2500.0
    physical_warn_limit_ratio: float = 0.08
    commit_warn_floor_mb: float = 6000.0
    commit_warn_limit_ratio: float = 0.12
    script_private_warn_floor_mb: float = 1024.0
    script_private_baseline_ratio: float = 2.0
    #: 启动预热窗口（秒）：期间的脚本内存峰值作为基线，2×基线作为告警阈值
    script_warmup_s: float = 120.0
    # --- T1 暂停（可恢复，仅“脚本控制中”生效） ------------------------------
    commit_pause_floor_mb: float = 2000.0
    commit_pause_limit_ratio: float = 0.05
    #: 60s 窗口内低于暂停阈值的占比下限
    commit_pause_occupancy: float = 0.8
    # --- T2 停止（终态） ----------------------------------------------------
    commit_stop_floor_mb: float = 1200.0
    commit_stop_limit_ratio: float = 0.025
    commit_stop_window_s: float = 30.0
    commit_stop_occupancy: float = 0.9
    #: 快速通道：Commit 余量极低时连续持续该秒数即停止（不等 30s 慢通道）
    commit_fast_stop_floor_mb: float = 400.0
    commit_fast_stop_limit_ratio: float = 0.01
    commit_fast_stop_sustained_s: float = 10.0
    # --- 清醒采样迟到（系统卡顿/线程饿死） ----------------------------------
    starvation_lateness_s: float = 2.5
    starvation_pause_events: int = 3
    starvation_stop_events: int = 3
    starvation_window_s: float = 120.0
    starvation_stop_window_s: float = 180.0
    # --- 睡眠/挂起与时间跳变 ------------------------------------------------
    #: 睡眠识别：``GTC64-QUT`` 偏移增量 ≥ max(该下限, 比例×当拍迟到) 时视为睡眠覆盖迟到
    sleep_detect_s: float = 1.0
    sleep_cover_ratio: float = 0.5
    sleep_grace_s: float = 120.0
    #: 墙上时间与单调时间差超过该值视为系统时间跳变（清窗）
    clock_jump_s: float = 2.0
    # --- 告警滞回与限频 -----------------------------------------------------
    #: 退出告警需要恢复幅度：上界指标 < 阈值×该比例；下界指标 > 阈值/该比例
    warn_clear_ratio: float = 0.8
    warn_clear_s: float = 60.0
    warn_repeat_interval_s: float = 300.0
    # --- 自动恢复 -----------------------------------------------------------
    pause_min_s: float = 60.0
    #: 恢复所需 Commit 余量 > 暂停阈值 × 该比例
    resume_commit_ratio: float = 1.5
    resume_occupancy: float = 0.9
    resume_quiet_s: float = 120.0
    #: 每个会话最多自动恢复次数（超过后不再自动恢复，交给人工）
    auto_resume_max: int = 2
    #: 该窗口（秒）内反复暂停一次即停止自动恢复
    auto_resume_repeat_window_s: float = 600.0


#: 过载保护合法模式（与 src/execution/overload_policy.py 的 OverloadMode 一致）
OVERLOAD_MODES: frozenset[str] = frozenset({"off", "monitor", "pause_stop", "stop"})


@dataclass(frozen=True, slots=True)
class TimeoutsConfig:
    queue_s: float = 600.0
    loading_s: float = 300.0
    exit_menu_s: float = 5.0
    exit_confirm_s: float = 5.0
    exit_s: float = 180.0
    port_s: float = 180.0
    popup_s: float = 60.0
    recovery_s: float = 30.0
    heartbeat_s: float = 10.0
    capture_ms: float = 300.0
    init_s: float = 30.0
    start_coop_s: float = 30.0
    port_state_s: float = 60.0
    dead_wait_s: float = 10.0
    battle_finished_s: float = 60.0
    stop_s: float = 10.0


@dataclass(frozen=True, slots=True)
class InputConfig:
    backend: str = "sendinput"
    min_action_interval_ms: int = 150
    click_hold_ms: tuple[int, int] = (30, 80)
    key_hold_ms: tuple[int, int] = (30, 60)
    interval_jitter: float = 0.2
    queue_max: int = 10
    dpi_aware: bool = True


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    screenshot_on_error: bool = True
    delete_screenshots_after_battle: bool = True
    log_max_bytes: int = 20 * 1024 * 1024
    log_backup_count: int = 5
    db_fallback_log: str = "logs/db_fallback.log"
    capture_stats: bool = True
    # 诊断截图保留策略（实现补充：区分临时战斗截图与异常留存，供误判复核）
    diagnostics_dir: str = "screenshots/diagnostics"
    diagnostics_max_files: int = 200
    diagnostics_max_mb: float = 100.0
    disk_warn_gb: float = 2.0
    disk_pause_gb: float = 0.5


@dataclass(frozen=True, slots=True)
class DeathConfig:
    weights: Mapping[str, float] = field(
        default_factory=lambda: {
            "hp_bar_absent": 0.35,
            "hp_zero": 0.25,
            "banner": 0.25,
            "marker_lost": 0.10,
            "spectator_ui": 0.05,
        }
    )
    score_threshold: float = 0.65  # 0.75 在 hp_zero 不可用时不可达，实机验证前用 0.65
    stable_s: float = 1.0
    strong_stable_s: float = 1.5
    marker_lost_stable_s: float = 3.0
    ignore_start_s: float = 5.0
    # 死亡提示只显示数秒，命中后锁存一段时间，避免提示消失后投票回落（评审 P0-7）
    banner_latch_s: float = 5.0


@dataclass(frozen=True, slots=True)
class PerceptionConfig:
    template_scales: tuple[float, ...] = (0.95, 1.0, 1.05)
    feature_confirm_frames: int = 2
    state_unknown_timeout_s: float = 2.0
    # 截屏方式（2026-10-31 实机修复）：auto=无边框全屏自动改用桌面 DC 截屏
    # （部分客户端窗口 BitBlt 黑屏、PrintWindow 返回冻结帧）；window=旧行为；screen=强制桌面截屏
    capture_method: str = "auto"
    orb_features_background: int = 2000
    orb_features_minimap: int = 1000
    orb_ratio_test: float = 0.75
    ransac_reproj_px: float = 3.0
    min_inliers: int = 15
    max_reprojection_error_px: float = 2.5
    map_scale_tolerance: float = 0.5
    map_rotation_tolerance_deg: float = 10.0
    map_candidate_margin: float = 0.30
    map_refresh_s: float = 30.0
    map_identify_retries: int = 3
    # 内点空间分布与二次验证（评审 P1-6）：凸包面积占内容区比例、warp 后边缘 NCC、
    # 刷新时新结果相对旧结果的内点提升与平移差阈值
    map_min_inlier_hull_ratio: float = 0.25
    map_verify_ncc: float = 0.30
    map_refresh_inlier_gain: float = 0.20
    map_refresh_translation_px: float = 5.0
    death: DeathConfig = field(default_factory=DeathConfig)


@dataclass(frozen=True, slots=True)
class AccountConfig:
    """一个本地账号档案（用户新增需求：多账号选择与轮换）。

    安全口径：只保存使用者自定义的**本地别名**与配置覆盖，不保存真实登录账号、
    密码、令牌，也不包含任何代登录信息；真实登录与切号仍由使用者在客户端手动完成。
    """

    alias: str
    display_name: str = ""
    profile: str | None = None  # 客户端配置 id，None 表示用命令行/全局默认
    ship_id: str | None = None  # 会话固定船只，None 表示用全局 ship.ship_id
    daily_battle_cap: int | None = None  # 覆盖 session.daily_battle_cap
    allowed_hours: tuple[str, str] | None = None  # 覆盖 session.allowed_hours
    max_battles_per_session: int | None = None  # 本账号单次会话最多场次
    enabled: bool = True
    notes: str = ""


@dataclass(frozen=True, slots=True)
class AccountsConfig:
    """账号档案集合与轮换策略。"""

    accounts: tuple[AccountConfig, ...] = ()
    rotation_enabled: bool = False
    rotation_mode: str = "round_robin"  # v1 仅 round_robin
    max_battles_per_account: int = 6  # 每个账号在一个轮换周期内最多打几场
    accounts_file: str = "accounts.yaml"

    def get(self, alias: str) -> AccountConfig | None:
        for account in self.accounts:
            if account.alias == alias:
                return account
        return None

    def enabled_accounts(self) -> tuple[AccountConfig, ...]:
        return tuple(account for account in self.accounts if account.enabled)


@dataclass(frozen=True, slots=True)
class GlobalConfig:
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    map: MapConfig = field(default_factory=MapConfig)
    navigation: NavigationConfig = field(default_factory=NavigationConfig)
    behavior: BehaviorConfig = field(default_factory=BehaviorConfig)
    ship: ShipConfig = field(default_factory=ShipConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    overload: OverloadConfig = field(default_factory=OverloadConfig)
    timeouts: TimeoutsConfig = field(default_factory=TimeoutsConfig)
    input: InputConfig = field(default_factory=InputConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    perception: PerceptionConfig = field(default_factory=PerceptionConfig)
    accounts: AccountsConfig = field(default_factory=AccountsConfig)
    project_root: Path | None = None
    config_path: Path | None = None


@dataclass(frozen=True, slots=True)
class AppConfig:
    """一次运行需要的全部配置与数据。"""

    global_config: GlobalConfig
    profile: ClientProfile
    keymap: KeyMap
    ship: ShipProfile
    maps: Mapping[str, MapPack]
    project_root: Path
    config_hash: str
    sources: Mapping[str, str] = field(default_factory=dict)
    account: AccountConfig | None = None

    def map_pack(self, map_id: str) -> MapPack:
        try:
            return self.maps[map_id]
        except KeyError as exc:
            raise KeyError(f"未加载地图包: {map_id}") from exc

    @property
    def account_alias(self) -> str | None:
        return self.account.alias if self.account else None

    @property
    def current_map(self) -> MapPack | None:
        forced = self.global_config.map.forced_map_id
        if forced:
            return self.maps.get(forced)
        if len(self.maps) == 1:
            return next(iter(self.maps.values()))
        return None
